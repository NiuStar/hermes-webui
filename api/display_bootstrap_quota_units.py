"""Render deployment units only; never installs, reloads or starts systemd.

The release directory must be root-protected and immutable during execution.
Generated units retain the host mount namespace required by quota admission.
Budgets are explicit deployment inputs, not invented production defaults.
"""
import re
from api.display_bootstrap_policy import BootstrapRejected


def render_units(*, release_path, creator_gid, memory_bytes, tasks_max,
                 stop_seconds):
    if (type(release_path) is not str or not re.fullmatch(r'/[A-Za-z0-9_./-]+', release_path)
            or any(p in ('', '.', '..') for p in release_path.split('/')[1:])):
        raise BootstrapRejected('INVALID_INPUT')
    for value in (creator_gid, memory_bytes, tasks_max, stop_seconds):
        if type(value) is not int or not 0 < value < 2**31:
            raise BootstrapRejected('INVALID_INPUT')
    socket = f'''[Unit]
Description=Protected offline bootstrap quota endpoint
Before=hermes-bootstrap-quota.service

[Socket]
ListenSequentialPacket=/etc/hermes-display-bootstrap/quota.sock
FileDescriptorName=quota
SocketUser=root
SocketGroup={creator_gid}
SocketMode=0660
DirectoryMode=0755
Accept=no
Service=hermes-bootstrap-quota.service
RemoveOnStop=no
Backlog=8

[Install]
WantedBy=sockets.target
'''
    service = f'''[Unit]
Description=Protected offline bootstrap quota broker
Requires=hermes-bootstrap-quota.socket
After=hermes-bootstrap-quota.socket

[Service]
Type=exec
User=root
Group=root
WorkingDirectory={release_path}
ExecStart=/usr/bin/python3 -I -B {release_path}/scripts/bootstrap_quota_entry.py
Sockets=hermes-bootstrap-quota.socket
UMask=0077
NoNewPrivileges=yes
# Keep host mount namespace: identities are approved against that namespace.
PrivateMounts=no
PrivateDevices=no
RestrictNamespaces=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictRealtime=yes
RestrictAddressFamilies=AF_UNIX
MemoryMax={memory_bytes}
MemorySwapMax=0
TasksMax={tasks_max}
TimeoutStopSec={stop_seconds}
KillMode=control-group
SendSIGKILL=yes
Restart=no
StandardInput=null
StandardOutput=null
StandardError=null
LimitCORE=0
UnsetEnvironment=NOTIFY_SOCKET JOURNAL_STREAM SYSLOG_IDENTIFIER
'''
    return {'hermes-bootstrap-quota.socket': socket,
            'hermes-bootstrap-quota.service': service}
