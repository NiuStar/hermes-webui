"""Single bounded bootstrap test service renderer; never installs or starts."""
import re
from api.display_bootstrap_test_budget import validate_policy

UNIT = 'hermes-bootstrap-tests.service'


def render(*, release, python, uid, gid, memory_bytes, tasks_max,
           runtime_seconds, stop_seconds, policy):
    validate_policy(policy)
    for value in (uid, gid, memory_bytes, tasks_max, runtime_seconds, stop_seconds):
        if type(value) is not int or not 0 < value < 2**31:
            raise ValueError('INVALID_INPUT')
    for path in (release, python):
        if (type(path) is not str or re.fullmatch('/[A-Za-z0-9_./-]+', path) is None
                or any(part in ('', '.', '..') for part in path.split('/')[1:])):
            raise ValueError('INVALID_INPUT')
    from pathlib import Path
    entry = Path(release) / 'scripts/bootstrap_test_entry.py'
    if not entry.is_file() or entry.is_symlink():
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    return {UNIT: f'''[Unit]
Description=Bounded isolated bootstrap verification
StartLimitIntervalSec=60
StartLimitBurst=5
StartLimitAction=none
[Service]
Type=exec
ExitType=main
User={uid}
Group={gid}
SupplementaryGroups=
WorkingDirectory={release}
ExecStart={python} -I -B -S {release}/scripts/bootstrap_test_entry.py
StandardInput=null
StandardOutput=null
StandardError=null
LimitCORE=0
UnsetEnvironment=NOTIFY_SOCKET JOURNAL_STREAM SYSLOG_IDENTIFIER
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
Delegate=no
ProtectControlGroups=yes
RestrictNamespaces=yes
RestrictSUIDSGID=yes
PrivateTmp=yes
UMask=0077
MemoryMax={memory_bytes}
MemorySwapMax=0
TasksMax={tasks_max}
RuntimeMaxSec={runtime_seconds}
TimeoutStopSec={stop_seconds}
KillMode=control-group
KillSignal=SIGTERM
FinalKillSignal=SIGKILL
SendSIGKILL=yes
Restart=no
'''}
