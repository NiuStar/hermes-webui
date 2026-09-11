"""Generate, never install, fixed socket-activated observer units."""
from pathlib import PurePosixPath
import re


def render(release, python, client_gid):
    if type(client_gid) is not int or client_gid <= 0:
        raise ValueError('INVALID_INPUT')
    for path in (release, python):
        if (type(path) is not str or not path.startswith('/')
                or re.fullmatch(r'/[a-zA-Z0-9_./-]+', path) is None
                or any(k in ('.', '..', '') for k in path.split('/')[1:])):
            raise ValueError('INVALID_INPUT')
    entry = str(PurePosixPath(release) / 'scripts/bootstrap_storage_entry.py')
    socket_unit = f'''[Unit]
Description=Offline bootstrap read-only storage observer socket
[Socket]
ListenStream=/run/hermes-display-bootstrap/storage.sock
SocketUser=root
SocketGroup={client_gid}
SocketMode=0660
DirectoryMode=0755
Accept=yes
PassCredentials=yes
MaxConnections=8
RemoveOnStop=no
[Install]
WantedBy=sockets.target
'''
    service = f'''[Unit]
Description=Offline bootstrap one-request storage observer
[Service]
Type=exec
User=root
Group=root
ExecStart={python} -I -B {entry}
Restart=no
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes
PrivateDevices=no
RestrictAddressFamilies=AF_UNIX
MemoryMax=134217728
MemorySwapMax=0
TasksMax=8
RuntimeMaxSec=15
KillMode=control-group
KillSignal=SIGKILL
FinalKillSignal=SIGKILL
SendSIGKILL=yes
UMask=0077
StandardInput=null
StandardOutput=null
StandardError=null
LimitCORE=0
UnsetEnvironment=NOTIFY_SOCKET JOURNAL_STREAM SYSLOG_IDENTIFIER
'''
    return {'hermes-bootstrap-storage.socket': socket_unit,
            'hermes-bootstrap-storage@.service': service}
