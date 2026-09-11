"""Protected one-connection socket-activated root storage observer entry."""
import os
from pathlib import Path
import socket
import stat
import sys


def main():
    if os.geteuid() != 0 or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise SystemExit('ACCESS_BOUNDARY_UNPROVEN')
    entry = Path(__file__).absolute()
    release = entry.parent.parent
    for path in [entry, entry.parent, release, *release.parents]:
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                or any(n.startswith('system.posix_acl_') for n in os.listxattr(path))):
            raise SystemExit('ACCESS_BOUNDARY_UNPROVEN')
    api = release / 'api'
    for path in [api, *api.rglob('*')]:
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                or any(n.startswith('system.posix_acl_') for n in os.listxattr(path))):
            raise SystemExit('ACCESS_BOUNDARY_UNPROVEN')
    if (os.environ.get('LISTEN_PID') != str(os.getpid())
            or os.environ.get('LISTEN_FDS') != '1'):
        raise SystemExit('ACCESS_BOUNDARY_UNPROVEN')
    sys.path.insert(0, str(release))
    from api.display_bootstrap_storage_server import handle
    with socket.socket(fileno=3) as connection:
        if connection.family != socket.AF_UNIX or connection.type != socket.SOCK_STREAM:
            raise SystemExit('ACCESS_BOUNDARY_UNPROVEN')
        connection.getpeername()  # Accept=yes must supply an already-connected fd.
        handle(connection)


if __name__ == '__main__':
    main()
