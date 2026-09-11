"""Checks application storage through a no-symlink directory-FD walk."""
import os
import stat
from pathlib import Path
from api.application_operation_issue import issue


def trusted_mkdir(path):
    return trusted_path(path, directory=True, _create=True)


def trusted_path(path, *, directory=False, _create=False):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise issue("INTEGRITY_ERROR", "storage", str(path), "absolute storage path required")
    fd = None
    try:
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        for index, part in enumerate(path.parts[1:]):
            last = index == len(path.parts) - 2
            is_dir = not last or directory
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
            if is_dir:
                flags |= os.O_DIRECTORY
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not _create or not is_dir:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            st = os.fstat(fd)
            # Root-owned sticky ancestors (e.g. /tmp) cannot replace another UID's child.
            sticky_ancestor = not last and st.st_uid == 0 and bool(st.st_mode & stat.S_ISVTX)
            kind_ok = stat.S_ISDIR(st.st_mode) if is_dir else stat.S_ISREG(st.st_mode)
            if (not kind_ok or st.st_uid not in {0, os.geteuid()}
                    or (st.st_mode & 0o022 and not sticky_ancestor)
                    or (not is_dir and st.st_nlink != 1)):
                raise issue("INTEGRITY_ERROR", "storage", str(path), "untrusted storage ancestry or permissions")
        st = os.fstat(fd)
        return st.st_dev, st.st_ino
    except OSError as exc:
        raise issue("INTEGRITY_ERROR", "storage", str(path), "storage path validation failed") from exc
    finally:
        if fd is not None:
            os.close(fd)
