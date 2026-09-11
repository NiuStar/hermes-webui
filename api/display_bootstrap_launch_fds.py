"""Strict descriptor manifest checked before the final output filter."""
import fcntl
import os
import socket
import stat
from api.display_bootstrap_manifest import digest, parse_record


def read_config_fd(fd, *, identity, sha, max_bytes=65536):
    """Root launcher provides exact read-only file identity and content SHA."""
    if (type(identity) is not dict or set(identity) != {'dev', 'ino', 'uid', 'gid', 'mode', 'nlink'}
            or type(max_bytes) is not int or not 1 <= max_bytes <= 65536):
        raise ValueError('INVALID_INPUT')
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
            or info.st_mode & 0o022 or not 0 < info.st_size <= max_bytes
            or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY
            or {k: getattr(info, 'st_'+k) for k in identity} != identity
            or any(k.startswith('system.posix_acl_') for k in os.listxattr(fd))):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    raw = os.pread(fd, max_bytes+1, 0)
    if len(raw) != info.st_size or digest(raw) != sha:
        raise ValueError('APPROVAL_MISMATCH')
    after = os.fstat(fd)
    if any(getattr(after, k) != getattr(info, k) for k in (
            'st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_nlink',
            'st_size', 'st_mtime_ns', 'st_ctime_ns')):
        raise ValueError('IDENTITY_CHANGED')
    return parse_record(raw)


def verify_fds(manifest):
    """manifest is a trusted exact fd→(purpose, dev, ino) map, not request data.

    Does not close unknown descriptors: caller exits and supervisor proves the
    original cgroup empty. A proc iterator's own closed FD is the only exception.
    """
    if type(manifest) is not dict or not {0, 1, 2} <= set(manifest):
        raise ValueError('INVALID_INPUT')
    observed = set()
    with os.scandir('/proc/self/fd') as entries:
        names = [entry.name for entry in entries]
    for name in names:
        fd = int(name)
        try:
            info = os.fstat(fd)
        except OSError:
            if fd in manifest:
                raise
            continue
        observed.add(fd)
        if fd not in manifest:
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        purpose, dev, ino = manifest[fd]
        if (info.st_dev, info.st_ino) != (dev, ino):
            raise ValueError('IDENTITY_CHANGED')
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        if purpose == 'config':
            valid = stat.S_ISREG(info.st_mode) and flags & os.O_ACCMODE == os.O_RDONLY
        elif purpose == 'directory':
            valid = stat.S_ISDIR(info.st_mode) and flags & os.O_ACCMODE == os.O_RDONLY
        elif purpose == 'lock':
            valid = (stat.S_ISREG(info.st_mode) and info.st_uid == 0
                     and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)
        elif purpose in ('control_read', 'control_write'):
            access = os.O_RDONLY if purpose == 'control_read' else os.O_WRONLY
            valid = stat.S_ISFIFO(info.st_mode) and flags & os.O_ACCMODE == access
        elif purpose in ('observer', 'quota'):
            with socket.socket(fileno=os.dup(fd)) as probe:
                valid = (probe.family == socket.AF_UNIX
                         and probe.type == (socket.SOCK_STREAM if purpose == 'observer' else socket.SOCK_SEQPACKET)
                         and probe.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 0)
                probe.getpeername()  # Reject unconnected descriptors.
        elif purpose == 'null' and fd in (0, 1, 2):
            valid = stat.S_ISCHR(info.st_mode) and info.st_rdev == os.makedev(1, 3)
        else:
            valid = False
        if not valid:
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        os.set_inheritable(fd, False)
    if observed != set(manifest):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
