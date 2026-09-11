"""Privileged broker-side peer inspection; never accepts client PID claims.

SO_PEERCRED supplies pid/uid/gid. The protected profile supplies the unit.
A live pidfd brackets the inspection to detect exit and PID reuse. Seccomp
status is necessary, not sufficient: worker guard installation is separate.
"""
import os
from pathlib import Path
import select
import subprocess
from contextlib import contextmanager


@contextmanager
def hold_peer(pid):
    """Keep the SO_PEERCRED process alive as an identity reference, not a PID.

    An open pidfd does not keep the process running. Poll before each privileged
    operation; a dead peer must never be replaced by a recycled numeric PID.
    """
    from api.display_bootstrap_policy import BootstrapRejected
    fd = os.pidfd_open(pid, 0)
    try:
        poller = select.poll()
        poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
        def alive():
            if poller.poll(0):
                raise BootstrapRejected('IDENTITY_CHANGED')
        alive()
        yield alive
    finally:
        os.close(fd)

from api.display_bootstrap_policy import BootstrapRejected
from api.display_bootstrap_runner import _systemd_properties, read_hard_limit_profile


def authenticate_peer(pid, uid, gid, profile_id):
    if os.geteuid() != 0 or type(pid) is not int or pid <= 0:
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
    pidfd = None
    try:
        pidfd = os.pidfd_open(pid, 0)
        poll = select.poll()
        poll.register(pidfd, select.POLLIN)
        if poll.poll(0):
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        profile, sha = read_hard_limit_profile(profile_id)
        if profile['format_version'] == 2:
            if profile['role'] not in ('creator', 'publisher', 'recover'):
                raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
            expected_uid, expected_gid = profile['uid'], profile['gid']
        else:
            expected_uid, expected_gid = profile['creator_uid'], profile['creator_gid']
        if uid != expected_uid or gid != expected_gid:
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        process = Path('/proc') / str(pid)
        before = (process / 'stat').read_text()
        start = before[before.rfind(')') + 2:].split()[19]
        status = dict(line.split(':', 1) for line in
                      (process / 'status').read_text().splitlines() if ':' in line)
        if ([int(v) for v in status['Uid'].split()] != [uid] * 4
                or [int(v) for v in status['Gid'].split()] != [gid] * 4
                or set(map(int, status['Groups'].split())) - {gid}
                or any(int(status[k], 16) for k in ('CapInh', 'CapPrm', 'CapEff', 'CapAmb'))
                or status['NoNewPrivs'].strip() != '1'
                or status['Seccomp'].strip() != '2'):
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        for key, namespace in [('mount_namespace', 'mnt'), ('user_namespace', 'user'),
                               ('cgroup_namespace', 'cgroup')]:
            if os.readlink(process / 'ns' / namespace) != profile[key]:
                raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        for name in ('uid_map', 'gid_map'):
            if (process / name).read_text().split() != ['0', '0', '4294967295']:
                raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        values = _systemd_properties(profile['unit'])
        expected = dict(Id=profile['unit'], MainPID=str(pid),
                        ControlGroup='/system.slice/' + profile['unit'],
                        LoadState='loaded', ActiveState='active', SubState='running',
                        Delegate='no', KillMode='control-group', SendSIGKILL='yes',
                        NoNewPrivileges='yes', User=str(uid), Group=str(gid), Type='exec',
                        KillSignal='9', FinalKillSignal='9', Restart='no',
                        RuntimeMaxUSec=profile['runtime_max_usec'])
        if values != expected:
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        if (process / 'cgroup').read_text().splitlines() != ['0::' + values['ControlGroup']]:
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        group = Path('/sys/fs/cgroup' + values['ControlGroup'])
        for name, expected_value in [('memory.max', profile['memory_max_bytes']),
                                      ('memory.swap.max', 0), ('pids.max', profile['pids_max'])]:
            if (group / name).read_text().strip() != str(expected_value):
                raise BootstrapRejected('RESOURCE_LIMIT')
        after = (process / 'stat').read_text()
        if (after[after.rfind(')') + 2:].split()[19] != start or poll.poll(0)
                or read_hard_limit_profile(profile_id) != (profile, sha)
                or _systemd_properties(profile['unit']) != values):
            raise BootstrapRejected('IDENTITY_CHANGED')
        return dict(pid=pid, start_ticks=start, profile_sha=sha)
    except BootstrapRejected:
        raise
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as exc:
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN') from exc
    finally:
        if pidfd is not None:
            os.close(pidfd)
