"""Safely align ownership within the WebUI home mount.

The Docker entrypoint runs this helper as root.  The walk stays anchored to
opened directory descriptors, never follows symlinks, and does not cross a
Linux mount boundary (including bind mounts that share st_dev).
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import os
import stat
from pathlib import Path, PurePosixPath

AT_EMPTY_PATH = 0x1000
AT_SYMLINK_NOFOLLOW = 0x100
O_PATH = getattr(os, "O_PATH", 0o10000000)
_AGENT_RELATIVE = PurePosixPath(".hermes/hermes-agent")
_MAX_CHOWN_ID = (1 << 32) - 2

_libc = ctypes.CDLL(None, use_errno=True)
_fchownat = _libc.fchownat
_fchownat.argtypes = [
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_uint,
    ctypes.c_uint,
    ctypes.c_int,
]
_fchownat.restype = ctypes.c_int


def _mount_id(fd: int) -> int:
    """Return Linux mount ID for an already-open descriptor, fail closed."""
    with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as info:
        for line in info:
            key, separator, value = line.partition(":")
            if separator and key == "mnt_id":
                return int(value.strip())
    raise RuntimeError(f"missing mnt_id for fd {fd}")


def _chown_fd(fd: int, uid: int, gid: int) -> None:
    """Change ownership of the opened object itself, including symlinks."""
    result = _fchownat(
        fd,
        b"",
        uid,
        gid,
        AT_EMPTY_PATH | AT_SYMLINK_NOFOLLOW,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _open_directory(parent_fd: int, name: str) -> int | None:
    """Open a child directory without following links; return None otherwise."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        if error.errno in {errno.ENOTDIR, errno.ELOOP}:
            return None
        raise


def _open_path(parent_fd: int, name: str) -> int:
    return os.open(
        name,
        O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )


def _walk_directory(
    directory_fd: int,
    relative: PurePosixPath,
    root_mount_id: int,
    uid: int,
    gid: int,
) -> None:
    for name in os.listdir(directory_fd):
        child_relative = relative / name
        if name == ".git" or child_relative == _AGENT_RELATIVE:
            continue

        try:
            child_fd = _open_directory(directory_fd, name)
            if child_fd is not None:
                try:
                    if _mount_id(child_fd) != root_mount_id:
                        continue
                    _chown_fd(child_fd, uid, gid)
                    _walk_directory(
                        child_fd,
                        child_relative,
                        root_mount_id,
                        uid,
                        gid,
                    )
                finally:
                    os.close(child_fd)
                continue

            child_fd = _open_path(directory_fd, name)
            try:
                child_stat = os.fstat(child_fd)
                if stat.S_ISDIR(child_stat.st_mode):
                    # The entry changed between the two openat calls. Retry via
                    # the parent descriptor instead of reopening by full path.
                    os.close(child_fd)
                    child_fd = -1
                    retry_fd = _open_directory(directory_fd, name)
                    if retry_fd is None:
                        raise RuntimeError("directory entry changed repeatedly")
                    try:
                        if _mount_id(retry_fd) != root_mount_id:
                            continue
                        _chown_fd(retry_fd, uid, gid)
                        _walk_directory(
                            retry_fd,
                            child_relative,
                            root_mount_id,
                            uid,
                            gid,
                        )
                    finally:
                        os.close(retry_fd)
                    continue
                if _mount_id(child_fd) != root_mount_id:
                    continue
                _chown_fd(child_fd, uid, gid)
            finally:
                if child_fd >= 0:
                    os.close(child_fd)
        except FileNotFoundError:
            # SQLite journals and other transient entries can disappear after
            # enumeration. No other filesystem error is suppressed.
            continue


def chown_home(home: Path, uid: int, gid: int) -> None:
    if not 0 <= uid <= _MAX_CHOWN_ID or not 0 <= gid <= _MAX_CHOWN_ID:
        raise ValueError("UID/GID outside supported chown range")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    home_fd = os.open(home, flags)
    try:
        root_mount_id = _mount_id(home_fd)
        _chown_fd(home_fd, uid, gid)
        _walk_directory(home_fd, PurePosixPath(), root_mount_id, uid, gid)
    finally:
        os.close(home_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("home", type=Path)
    parser.add_argument("uid", type=int)
    parser.add_argument("gid", type=int)
    args = parser.parse_args(argv)
    chown_home(args.home, args.uid, args.gid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
