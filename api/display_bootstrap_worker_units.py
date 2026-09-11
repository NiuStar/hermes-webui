"""Generate single-operation worker units; never install or start services.

One protected hard-limit profile names one unit. Run creator and publisher
sequentially using the same approved unit identity and a fresh process each time.
The administrator supplies the external approval; this renderer never issues it.
"""
import re
from api.display_bootstrap_policy import BootstrapRejected
from api.display_bootstrap_runner import _validate_profile


def render_worker_unit(*, release_path, profile, policy_id, operation,
                       candidate_id=None, approval_id=None, manifest_sha=None,
                       policy_sha=None, reference=None):
    if (type(release_path) is not str
            or re.fullmatch(r'/[A-Za-z0-9_./-]+', release_path) is None
            or any(p in ('', '.', '..') for p in release_path.split('/')[1:])):
        raise BootstrapRejected('INVALID_INPUT')
    if type(profile) is not dict:
        raise BootstrapRejected('INVALID_INPUT')
    profile_id = profile.get('hard_limit_profile_id')
    for value in (profile_id, policy_id):
        if type(value) is not str or re.fullmatch('[0-9a-f]{32}', value) is None:
            raise BootstrapRejected('INVALID_INPUT')
    _validate_profile(profile, profile_id)
    if operation not in ('create', 'publish', 'recover', 'approve'):
        raise BootstrapRejected('INVALID_INPUT')
    if operation == 'approve':
        if profile['format_version'] != 2:
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        for value in (manifest_sha, policy_sha):
            if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
                raise BootstrapRejected('INVALID_INPUT')
        if (type(reference) is not str or not reference.strip()
                or len(reference.encode('utf-8')) > 4096
                or any(ord(c) < 32 or ord(c) == 127 for c in reference)):
            raise BootstrapRejected('INVALID_INPUT')
    elif any(value is not None for value in (manifest_sha, policy_sha, reference)):
        raise BootstrapRejected('INVALID_INPUT')
    for value, required in ((candidate_id, True),
                            (approval_id, operation in ('publish', 'approve'))):
        if required:
            if type(value) is not str or re.fullmatch('[0-9a-f]{32}', value) is None:
                raise BootstrapRejected('INVALID_INPUT')
        elif value is not None:
            raise BootstrapRejected('INVALID_INPUT')
    if profile['format_version'] == 2:
        expected_role = {'create': 'creator', 'publish': 'publisher',
                         'recover': 'recover', 'approve': 'approver'}[operation]
        if profile['role'] != expected_role:
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        uid, gid = profile['uid'], profile['gid']
        groups = ' '.join(str(g) for g in profile['supplementary_gids'])
    else:
        uid, gid = profile['creator_uid'], profile['creator_gid']
        groups = ''
    args = (f'approve --policy-id {policy_id}' if operation == 'approve'
            else f'--policy-id {policy_id} {operation}')
    if candidate_id is not None:
        args += f' --candidate-id {candidate_id}'
    if approval_id is not None:
        args += f' --approval-id {approval_id}'
    if operation == 'approve':
        quoted = reference.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$')
        args += f' --manifest-sha {manifest_sha} --policy-sha {policy_sha} --reference "{quoted}"'
    dependencies = ('' if operation == 'approve' else
                    'Requires=hermes-bootstrap-quota.socket hermes-bootstrap-storage.socket\n'
                    'After=hermes-bootstrap-quota.socket hermes-bootstrap-storage.socket\n')
    umask = '0027' if operation == 'approve' else '0077'
    text = f'''[Unit]
Description=Offline bootstrap single-operation worker
StartLimitIntervalSec=60
StartLimitBurst=5
StartLimitAction=none
{dependencies}

[Service]
Type=exec
User={uid}
Group={gid}
SupplementaryGroups={groups}
WorkingDirectory={release_path}
ExecStart=/usr/bin/python3 -I -B -S {release_path}/scripts/bootstrap_worker_entry.py {args}
UMask={umask}
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
Delegate=no
PrivateMounts=no
PrivateDevices=no
RestrictNamespaces=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictRealtime=yes
MemoryMax={profile['memory_max_bytes']}
MemorySwapMax=0
TasksMax={profile['pids_max']}
RuntimeMaxSec={profile['runtime_max_usec']}us
TimeoutStopSec=0
KillMode=control-group
KillSignal=SIGKILL
FinalKillSignal=SIGKILL
SendSIGKILL=yes
Restart=no
StandardInput=null
StandardOutput=null
StandardError=null
LimitCORE=0
UnsetEnvironment=NOTIFY_SOCKET JOURNAL_STREAM SYSLOG_IDENTIFIER
'''
    return {profile['unit']: text}
