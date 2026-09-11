"""Isolated non-root offline worker; no service installation or activation."""
import os
from pathlib import Path
import stat
import sys


def _protected(path, *, directory=False):
    info = path.lstat()
    if (info.st_uid != 0 or info.st_mode & 0o022
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)
            or any(n.startswith('system.posix_acl_') for n in os.listxattr(path))):
        raise SystemExit('ACCESS_BOUNDARY_UNPROVEN')


def main():
    if os.geteuid() == 0 or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise SystemExit('ACCESS_BOUNDARY_UNPROVEN')
    entry = Path(__file__).absolute()
    release = entry.parent.parent
    _protected(entry)
    for path in (entry.parent, release, *release.parents):
        _protected(path, directory=True)
    # Inspect before importing project code; no symlink traversal. Deployment
    # administrators must keep the root-owned release immutable during execution.
    pending = [release / 'api']
    count = 0
    while pending:
        directory = pending.pop()
        _protected(directory, directory=True)
        with os.scandir(directory) as entries:
            for item in entries:
                count += 1
                if count > 10000:
                    raise SystemExit('ACCESS_BOUNDARY_UNPROVEN')
                path = Path(item.path)
                if item.is_dir(follow_symlinks=False):
                    _protected(path, directory=True)
                    pending.append(path)
                else:
                    _protected(path)
    sys.path.insert(0, str(release))
    if len(sys.argv) > 1 and sys.argv[1] == 'approve':
        from api.display_bootstrap_approval_command import main as command
        return command(sys.argv[2:])
    from api.display_bootstrap_command import main as command
    return command()


if __name__ == '__main__':
    raise SystemExit(main())
