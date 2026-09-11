"""Target-identity fixed endpoint connection during trusted startup only."""
import os
import socket
import stat
import struct
import time
from api.display_bootstrap_launch_identity import verify_identity

ENDPOINTS = {
    'observer': ('/run/hermes-display-bootstrap', 'storage.sock', socket.SOCK_STREAM),
    'quota': ('/etc/hermes-display-bootstrap', 'quota.sock', socket.SOCK_SEQPACKET),
}


def connect(role, root_fd, *, uid, gid, supplementary_gids, endpoint_identity,
            expected_peer, deadline, verify_peer):
    """Called before final filter, no caller-controlled path, no business payload."""
    if role not in ENDPOINTS:
        raise ValueError('INVALID_INPUT')
    root_path, name, kind = ENDPOINTS[role]
    verify_identity(uid, gid, supplementary_gids)
    root = os.fstat(root_fd)
    path = os.stat(root_path, follow_symlinks=False)
    if (not stat.S_ISDIR(root.st_mode) or root.st_uid != 0 or root.st_mode & 0o022
            or (root.st_dev, root.st_ino) != (path.st_dev, path.st_ino)):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    fields = ('dev', 'ino', 'uid', 'gid', 'mode')
    if type(endpoint_identity) is not dict or set(endpoint_identity) != set(fields):
        raise ValueError('INVALID_INPUT')
    def unchanged():
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0
                or {k: getattr(info, 'st_'+k) for k in fields} != endpoint_identity):
            raise ValueError('IDENTITY_CHANGED')
        verify_identity(uid, gid, supplementary_gids)
        if time.monotonic() >= deadline:
            raise ValueError('RESOURCE_LIMIT')
    unchanged()
    connection = socket.socket(socket.AF_UNIX, kind)
    try:
        if role == 'observer':
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('RESOURCE_LIMIT')
        connection.settimeout(remaining)
        connection.connect('/proc/self/fd/%d/%s' % (root_fd, name))
        unchanged()
        peer = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if peer != expected_peer or peer[0] <= 0 or peer[1:] != (0, 0):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        verify_peer(peer)
        unchanged()
        return connection
    except BaseException:
        connection.close()
        raise
