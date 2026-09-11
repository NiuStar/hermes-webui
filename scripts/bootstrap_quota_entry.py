"""Isolated-interpreter entry for an explicitly installed quota broker.

No shell/environment Python path is used. Validate release ancestry and source
ownership before importing application modules. This file does not install units.
"""
import os
from pathlib import Path
import stat
import sys


def main():
    if os.geteuid() != 0 or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise SystemExit('ACCESS_BOUNDARY_UNPROVEN')
    entry = Path(__file__).absolute()
    release = entry.parent.parent
    # lstat before resolve: accepting a resolved symlink would erase the evidence.
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
    sys.path.insert(0, str(release))
    from api.display_bootstrap_quota_daemon import serve
    serve()


if __name__ == '__main__':
    main()
