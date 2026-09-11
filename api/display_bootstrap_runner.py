"""Live systemd runner checks, NOT volume admission or a reusable capability.

Root/host administrators and exclusive use of the dedicated UID are trusted.
No environment proof, caller-selected configuration path or cached attestation.
Only host cgroup-v2/system systemd with nondelegated services is supported.
"""
import os
import re
import subprocess
from pathlib import Path

from api.display_bootstrap_policy import (
    BootstrapRejected, open_protected_root, read_protected_record,
    verify_process_identity,
)
from api.display_bootstrap_manifest import parse_record, digest

_PROFILE_ROOT = '/etc/hermes-display-bootstrap/hard-limits'


def _id(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{32}', value) is None:
        raise BootstrapRejected('INVALID_INPUT')


def _validate_profile(value, profile_id):
    if (type(value) is dict and type(value.get('format_version')) is int
            and value['format_version'] == 2):
        extra = {'role', 'uid', 'gid', 'supplementary_gids'}
        if not extra <= value.keys() or 'creator_uid' in value or 'creator_gid' in value:
            raise BootstrapRejected('INVALID_INPUT')
        if value['role'] not in ('creator', 'publisher', 'recover', 'approver'):
            raise BootstrapRejected('INVALID_INPUT')
        groups = value['supplementary_gids']
        if (type(groups) is not list or any(type(g) is not int or g <= 0 for g in groups)
                or groups != sorted(set(groups)) or value['gid'] in groups
                or (value['role'] == 'approver' and len(groups) != 1)
                or (value['role'] != 'approver' and groups)):
            raise BootstrapRejected('INVALID_INPUT')
        shared = {k: v for k, v in value.items() if k not in extra}
        shared.update(format_version=1, creator_uid=value['uid'], creator_gid=value['gid'])
        _validate_profile(shared, profile_id)
        return value
    fields = {'format_version', 'hard_limit_profile_id', 'unit', 'creator_uid',
              'creator_gid', 'memory_max_bytes', 'memory_swap_max_bytes',
              'pids_max', 'runtime_max_usec', 'mount_namespace',
              'cgroup_namespace', 'user_namespace'}
    if type(value) is not dict or set(value) != fields:
        raise BootstrapRejected('INVALID_INPUT')
    namespaces = {'mount_namespace': 'mnt', 'cgroup_namespace': 'cgroup', 'user_namespace': 'user'}
    for key, name in namespaces.items():
        if type(value[key]) is not str or re.fullmatch(name + r':\[[0-9]+\]', value[key]) is None:
            raise BootstrapRejected('INVALID_INPUT')
    for key in fields - {'hard_limit_profile_id', 'unit'} - namespaces.keys():
        if type(value[key]) is not int or not 0 <= value[key] <= 9007199254740991:
            raise BootstrapRejected('INVALID_INPUT')
    if (value['format_version'] != 1 or value['hard_limit_profile_id'] != profile_id
            or value['creator_uid'] == 0 or value['creator_gid'] == 0
            or value['memory_swap_max_bytes'] != 0
            or any(value[k] < 1 for k in ('memory_max_bytes', 'pids_max', 'runtime_max_usec'))
            or type(value['unit']) is not str
            or re.fullmatch('hermes-bootstrap-[a-z0-9-]{1,96}\\.service', value['unit']) is None):
        raise BootstrapRejected('INVALID_INPUT')
    return value


def read_hard_limit_profile(profile_id):
    """Fixed root-owned canonical configuration; ID is only a lookup assertion."""
    _id(profile_id)
    fd = open_protected_root(_PROFILE_ROOT, {0})
    try:
        raw = read_protected_record(fd, profile_id + '.json', {0})
        try:
            profile = _validate_profile(parse_record(raw), profile_id)
        except ValueError as exc:
            if isinstance(exc, BootstrapRejected):
                raise
            raise BootstrapRejected('INVALID_INPUT') from exc
        return profile, digest(raw)
    finally:
        os.close(fd)


def _systemd_properties(unit):
    keys = ('Id', 'MainPID', 'ControlGroup', 'LoadState', 'ActiveState',
            'SubState', 'Delegate', 'KillMode', 'SendSIGKILL', 'NoNewPrivileges',
            'User', 'Group', 'Type', 'KillSignal', 'FinalKillSignal', 'Restart')
    command = ['/usr/bin/systemctl', '--system', 'show', unit, '--no-pager']
    command += ['--property=' + key for key in keys]
    output = subprocess.run(command, check=True, capture_output=True, text=True,
                            timeout=5, env={'LC_ALL': 'C', 'PATH': '/usr/bin:/bin'})
    values = {}
    for line in output.stdout.splitlines():
        key, value = line.split('=', 1)
        if key in values or key not in keys:
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        values[key] = value
    if set(values) != set(keys):
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
    # D-Bus returns typed integer microseconds, not localized timespan text.
    import json
    escaped = ''.join(c if c.isascii() and c.isalnum() else '_%02x' % ord(c) for c in unit)
    result = subprocess.run([
        '/usr/bin/busctl', '--system', '--json=short', 'get-property',
        'org.freedesktop.systemd1', '/org/freedesktop/systemd1/unit/' + escaped,
        'org.freedesktop.systemd1.Service', 'RuntimeMaxUSec'],
        check=True, capture_output=True, text=True, timeout=5,
        env={'LC_ALL': 'C', 'PATH': '/usr/bin:/bin'})
    runtime = json.loads(result.stdout)
    if runtime.get('type') != 't' or type(runtime.get('data')) is not int:
        raise BootstrapRejected('UNSUPPORTED_PLATFORM')
    values['RuntimeMaxUSec'] = runtime['data']
    return values


def _self_cgroup():
    lines = Path('/proc/self/cgroup').read_text().splitlines()
    if len(lines) != 1 or re.fullmatch(r'0::(/[a-zA-Z0-9_.@\\x2d-]+)+', lines[0]) is None:
        raise BootstrapRejected('UNSUPPORTED_PLATFORM')
    relative = lines[0][3:]
    if any(part in ('', '.', '..') for part in relative.split('/')[1:]):
        raise BootstrapRejected('UNSUPPORTED_PLATFORM')
    return relative


def _check_cgroup(profile, relative):
    # Host namespaces are pinned by root: unprivileged UID cannot read PID1 ns.
    for key, ns in (('mount_namespace', 'mnt'), ('cgroup_namespace', 'cgroup'),
                    ('user_namespace', 'user')):
        if os.readlink('/proc/self/ns/' + ns) != profile[key]:
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
    mounts = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
    groups = [row for row in mounts if row[row.index('-') + 1] == 'cgroup2']
    if len(groups) != 1 or groups[0][3:5] != ['/', '/sys/fs/cgroup']:
        raise BootstrapRejected('UNSUPPORTED_PLATFORM')
    group = Path('/sys/fs/cgroup' + relative)
    fd = open_protected_root(str(group), {0})
    try:
        for name, expected in [('memory.max', profile['memory_max_bytes']),
                               ('memory.swap.max', 0), ('pids.max', profile['pids_max'])]:
            raw = (group / name).read_text().strip()
            if raw != str(expected):
                raise BootstrapRejected('RESOURCE_LIMIT')
        if (group / 'cgroup.type').read_text().strip() != 'domain':
            raise BootstrapRejected('UNSUPPORTED_PLATFORM')
        # Moving out requires write permission on the common ancestor's procs.
        # Refuse delegation, writable ancestors and writable limit/migration files.
        for parent in [group, *group.parents]:
            if not str(parent).startswith('/sys/fs/cgroup'):
                break
            for name in ('', 'cgroup.procs', 'cgroup.threads', 'cgroup.subtree_control',
                         'memory.max', 'memory.swap.max', 'pids.max'):
                target = parent / name
                if target.exists() and os.access(target, os.W_OK, effective_ids=True):
                    raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        return dict(cgroup=relative, memory_max=profile['memory_max_bytes'],
                    swap_max=0, pids_max=profile['pids_max'])
    finally:
        os.close(fd)


def verify_systemd_runner(profile_id, *, max_rss_bytes, max_elapsed_seconds,
                          config=None, role=None):
    """Read and verify now; returned audit dict is NOT admission/capability.

    Caller must separately prove volume quotas/audit reserve and reacquire all
    checks at lifecycle boundaries. No SQLite/file creation is performed here.
    """
    _id(profile_id)
    for value in (max_rss_bytes, max_elapsed_seconds):
        if type(value) is not int or not 0 < value <= 9007199254740991:
            raise BootstrapRejected('INVALID_INPUT')
    try:
        profile, sha = read_hard_limit_profile(profile_id)
        if profile['format_version'] == 2:
            from api.display_bootstrap_roles import verify_identity
            if config is None or role != profile['role']:
                raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
            identity = verify_identity(config, role)
            uid, gid = profile['uid'], profile['gid']
            if (identity['uid'] != uid or identity['gid'] != gid
                    or identity['supplementary_gids'] != profile['supplementary_gids']):
                raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        else:
            if config is not None and config.get('format_version') in (2, 3):
                raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
            uid, gid = profile['creator_uid'], profile['creator_gid']
            verify_process_identity({'creator_uid': uid, 'approver_uid': 0})
            if os.getresgid() != (gid,) * 3:
                raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        if (profile['memory_max_bytes'] > max_rss_bytes
                or profile['runtime_max_usec'] > max_elapsed_seconds * 1000000):
            raise BootstrapRejected('RESOURCE_LIMIT')
        relative = _self_cgroup()
        values = _systemd_properties(profile['unit'])
        expected = dict(Id=profile['unit'], MainPID=str(os.getpid()), ControlGroup=relative,
                        LoadState='loaded', ActiveState='active', SubState='running',
                        Delegate='no', KillMode='control-group', SendSIGKILL='yes',
                        NoNewPrivileges='yes', User=str(uid),
                        Group=str(gid), Type='exec',
                        KillSignal='9', FinalKillSignal='9', Restart='no',
                        RuntimeMaxUSec=profile['runtime_max_usec'])
        if values != expected:
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        observation = _check_cgroup(profile, relative)
        if _self_cgroup() != relative or _systemd_properties(profile['unit']) != values:
            raise BootstrapRejected('IDENTITY_CHANGED')
        if read_hard_limit_profile(profile_id) != (profile, sha):
            raise BootstrapRejected('IDENTITY_CHANGED')
        from api.display_bootstrap_seccomp import install_quota_guard, verify_quota_guard
        try:
            install_quota_guard()
            guard = verify_quota_guard()
        except (RuntimeError, OSError) as exc:
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN') from exc
        return dict(observation, **guard, pid=os.getpid(), unit=profile['unit'],
                    profile_sha=sha, runtime_max_usec=profile['runtime_max_usec'])
    except BootstrapRejected:
        raise
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN') from exc
