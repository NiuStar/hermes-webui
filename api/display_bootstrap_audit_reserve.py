"""Preallocated audit slots, no automatic deletion or space-release fallback.

Low-level building block, NOT volume admission. The caller must hold the
lifecycle lock, prove exclusive audit ownership and independent filesystem,
and reserve metadata headroom separately. A failed/claimed slot is retained
and never recycled. Provision before creating any candidate data.
"""
import errno
import os
import re
import stat

from api.display_bootstrap_manifest import parse_record
from api.display_bootstrap_policy import BootstrapRejected
from api.display_bootstrap_publish import rename_no_replace

SLOT_BYTES = 65536
_SLOT = re.compile(r'slot-[0-9]{8}')
_CLAIM = re.compile(r'claimed-[0-9]{8}')


def _directory(fd):
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or any(n.startswith('system.posix_acl_') for n in os.listxattr(fd))):
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
    return info


def _slot(fd, owner):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size != SLOT_BYTES or info.st_blocks * 512 < SLOT_BYTES
            or any(n.startswith('system.posix_acl_') for n in os.listxattr(fd))):
        raise BootstrapRejected('AUDIT_UNAVAILABLE')
    return info


MAX_SLOTS = 4096


def validate_budget(reserve_bytes, *, minimum_slots=1):
    """Validate without touching storage; never round an approved budget."""
    if (type(minimum_slots) is not int or not 1 <= minimum_slots <= MAX_SLOTS
            or type(reserve_bytes) is not int
            or not minimum_slots * SLOT_BYTES <= reserve_bytes <= MAX_SLOTS * SLOT_BYTES
            or reserve_bytes % SLOT_BYTES):
        raise BootstrapRejected('INVALID_INPUT')
    return reserve_bytes // SLOT_BYTES


def _names(directory_fd):
    # Bound memory even for a corrupted reserve directory. scandir duplicates
    # the caller's FD; closing the iterator does not consume caller ownership.
    names = []
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            if len(names) == MAX_SLOTS:
                raise BootstrapRejected('AUDIT_UNAVAILABLE')
            names.append(entry.name)
    return sorted(names)


def provision(directory_fd, *, reserve_bytes):
    """Populate an already-created empty 0700 reserve directory exclusively.

    Partial provisioning remains on failure. Never retry provisioning over it.
    Budget must be exact slot units, so no rounding silently exceeds approval.
    """
    count = validate_budget(reserve_bytes)
    owner = _directory(directory_fd).st_uid
    if _names(directory_fd):
        raise BootstrapRejected('STATE_CONFLICT')
    for index in range(count):
        fd = os.open(f'slot-{index:08d}', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory_fd)
        try:
            os.posix_fallocate(fd, 0, SLOT_BYTES)
            os.fsync(fd)
            _slot(fd, owner)
        finally:
            os.close(fd)
        os.fsync(directory_fd)
    return inspect(directory_fd)


def _check_named(directory_fd, name, fd, before):
    fields = ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode', 'st_nlink',
              'st_size', 'st_blocks', 'st_mtime_ns', 'st_ctime_ns')
    after = os.fstat(fd)
    named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if any(getattr(before, field) != getattr(other, field)
           for other in (after, named) for field in fields):
        raise BootstrapRejected('IDENTITY_CHANGED')


def inspect(directory_fd):
    """Read actual allocated blocks; never count consumed or failed slots."""
    owner = _directory(directory_fd).st_uid
    available = []
    claimed = []
    indices = set()
    names = _names(directory_fd)
    for name in names:
        if not (_CLAIM.fullmatch(name) or _SLOT.fullmatch(name)):
            raise BootstrapRejected('STATE_CONFLICT')
        index = int(name.rsplit('-', 1)[1])
        if index >= MAX_SLOTS or index in indices:
            raise BootstrapRejected('STATE_CONFLICT')
        indices.add(index)
    for name in names:
        if _CLAIM.fullmatch(name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory_fd)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner
                        or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                        or not 0 <= info.st_size <= SLOT_BYTES
                        or any(n.startswith('system.posix_acl_') for n in os.listxattr(fd))):
                    raise BootstrapRejected('IDENTITY_CHANGED')
                _check_named(directory_fd, name, fd, info)
            finally:
                os.close(fd)
            claimed.append(name)
            continue
        if not _SLOT.fullmatch(name):
            raise BootstrapRejected('STATE_CONFLICT')
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        try:
            info = _slot(fd, owner)
            _check_named(directory_fd, name, fd, info)
        finally:
            os.close(fd)
        available.append(name)
    if _names(directory_fd) != names:
        raise BootstrapRejected('IDENTITY_CHANGED')
    return dict(available_bytes=len(available) * SLOT_BYTES,
                available_slots=available, retained_claims=claimed)


def write_reserved(reserve_fd, target_fd, name, raw):
    """Consume one slot without releasing its allocation before the write.

    Atomic rename claims the inode before touching it. On crash the claimed
    inode remains, never treated as clean capacity. Final publication is
    no-replace and both directories are synced. Metadata ENOSPC still blocks.
    """
    owner = _directory(reserve_fd).st_uid
    target = os.fstat(target_fd)
    if (not stat.S_ISDIR(target.st_mode) or target.st_dev != os.fstat(reserve_fd).st_dev
            or target.st_uid != owner or target.st_mode & 0o022
            or any(n.startswith('system.posix_acl_') for n in os.listxattr(target_fd))):
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
    if (type(name) is not str or not name or name in ('.', '..') or '/' in name
            or '\x00' in name or type(raw) is not bytes or not 0 < len(raw) <= SLOT_BYTES):
        raise BootstrapRejected('INVALID_INPUT')
    parse_record(raw)
    slots = inspect(reserve_fd)['available_slots']
    if not slots:
        raise BootstrapRejected('AUDIT_UNAVAILABLE')
    source = slots[0]
    claimed = 'claimed-' + source[5:]
    rename_no_replace(reserve_fd, source, reserve_fd, claimed)
    os.fsync(reserve_fd)
    fd = os.open(claimed, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=reserve_fd)
    try:
        before = _slot(fd, owner)
        remaining = memoryview(raw)
        offset = 0
        while remaining:
            count = os.pwrite(fd, remaining, offset)
            if count <= 0:
                raise OSError(errno.EIO, 'short audit write')
            offset += count
            remaining = remaining[count:]
        os.fsync(fd)
        # Only release unused tail after the record bytes have been synced.
        os.ftruncate(fd, len(raw))
        os.fsync(fd)
        if os.pread(fd, SLOT_BYTES + 1, 0) != raw:
            raise BootstrapRejected('AUDIT_UNAVAILABLE')
        named = os.stat(claimed, dir_fd=reserve_fd, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino):
            raise BootstrapRejected('IDENTITY_CHANGED')
        rename_no_replace(reserve_fd, claimed, target_fd, name)
        os.fsync(target_fd)
        os.fsync(reserve_fd)
        check = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=target_fd)
        try:
            actual = os.fstat(check)
            if ((actual.st_dev, actual.st_ino) != (before.st_dev, before.st_ino)
                    or actual.st_nlink != 1 or actual.st_size != len(raw)
                    or os.pread(check, SLOT_BYTES + 1, 0) != raw):
                raise BootstrapRejected('AUDIT_UNAVAILABLE')
        finally:
            os.close(check)
    finally:
        os.close(fd)
