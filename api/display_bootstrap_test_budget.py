"""Test-history accounting. No process start, deletion or automatic budget reset."""
import os
import stat
from api.display_bootstrap_v4 import checked

FIELDS = frozenset('version roots max_total_bytes max_total_inodes max_total_files '
    'max_batch_bytes max_batch_inodes max_batch_files min_free_bytes max_log_bytes'.split())


def validate_policy(value):
    from api.display_bootstrap_manifest import canonical_bytes, validate_record
    canonical_bytes(value)
    if type(value) is not dict or set(value) != FIELDS:
        raise ValueError('INVALID_INPUT')
    for name in FIELDS - {'roots'}:
        if checked(value[name]) < 1:
            raise ValueError('INVALID_INPUT')
    if value['version'] != 1 or type(value['roots']) is not dict or not value['roots']:
        raise ValueError('INVALID_INPUT')
    for path, identity in value['roots'].items():
        if not path.startswith('/') or any(x in ('', '.', '..') for x in path.split('/')[1:]):
            raise ValueError('INVALID_INPUT')
        validate_record(identity, 'directory_identity')
    for axis in ('bytes', 'inodes', 'files'):
        if value['max_batch_' + axis] >= value['max_total_' + axis]:
            raise ValueError('INVALID_INPUT')
    if value['max_log_bytes'] > value['max_batch_bytes']:
        raise ValueError('INVALID_INPUT')
    return value


def inventory(root_fds, *, max_entries, check_deadline):
    """Deduplicate registered nested roots/hardlinks; reject symlinks/specials."""
    if type(max_entries) is not int or max_entries < 1 or not callable(check_deadline):
        raise ValueError('INVALID_INPUT')
    seen = set()
    totals = dict(bytes=0, inodes=0, files=0)
    visits = 0

    def walk(fd):
        nonlocal visits
        check_deadline()
        info = os.fstat(fd)
        key = info.st_dev, info.st_ino
        if key in seen:
            return
        seen.add(key)
        visits += 1
        if visits > max_entries:
            raise ValueError('RESOURCE_LIMIT')
        totals['bytes'] = checked(totals['bytes'] + checked(info.st_blocks * 512))
        totals['inodes'] = checked(totals['inodes'] + 1)
        if stat.S_ISREG(info.st_mode):
            totals['files'] = checked(totals['files'] + 1)
            return
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('STATE_CONFLICT')
        with os.scandir(fd) as entries:
            for entry in entries:
                check_deadline()
                child = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                                | os.O_CLOEXEC, dir_fd=fd)
                try:
                    walk(child)
                finally:
                    os.close(child)
        after = os.fstat(fd)
        if (after.st_mtime_ns, after.st_ctime_ns) != (info.st_mtime_ns, info.st_ctime_ns):
            raise ValueError('IDENTITY_CHANGED')
    for fd in root_fds:
        walk(fd)
    return totals


def require_batch(policy, occupied, reserved, free_bytes):
    validate_policy(policy)
    checked(free_bytes)
    for row in (occupied, reserved):
        if type(row) is not dict or set(row) != {'bytes', 'inodes', 'files'}:
            raise ValueError('INVALID_INPUT')
        for value in row.values():
            checked(value)
    for axis in ('bytes', 'inodes', 'files'):
        amount = checked(checked(occupied[axis] + reserved[axis]) + policy['max_batch_' + axis])
        if amount >= policy['max_total_' + axis]:
            raise ValueError('RESOURCE_LIMIT')
    needed = checked(checked(reserved['bytes'] + policy['max_batch_bytes']) + policy['min_free_bytes'])
    if free_bytes <= needed:
        raise ValueError('RESOURCE_LIMIT')
