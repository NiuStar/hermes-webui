"""Linux no-replace primitive; not a publication authorization interface."""
import ctypes
import errno
import os
import sys
import secrets

from api.display_bootstrap_manifest import parse_record, validate_record, digest


def recovery_action(state, *, source_exists, target_exists, approval_matches):
    """Pure decision only: callers must verify identities before any action."""
    if state == 'FAILED':
        return 'BLOCKED_TERMINAL'
    if state == 'QUARANTINED':
        return 'QUARANTINED'
    if state in ('RESERVED', 'BUILDING'):
        return 'FAIL_INTERRUPTED_BUILD'
    if state == 'VERIFIED':
        return 'WAIT_EXPLICIT_APPROVAL'
    if state not in ('APPROVED', 'PUBLISH_INTENT', 'PUBLISHED_UNACTIVATED'):
        raise ValueError('STATE_CONFLICT')
    if type(source_exists) is not bool or type(target_exists) is not bool:
        raise ValueError('IO_FAILURE')
    if (source_exists == target_exists
            or (state == 'PUBLISHED_UNACTIVATED' and source_exists)
            or (state == 'APPROVED' and target_exists)):
        return 'QUARANTINE_CONFLICT'
    if approval_matches is not True:
        return 'BLOCKED_APPROVAL'
    if state == 'APPROVED':
        return 'WRITE_INTENT'
    if source_exists:
        return 'RENAME'
    if state == 'PUBLISH_INTENT':
        return 'SYNC_AND_COMPLETE'
    return 'READBACK_COMPLETED'


def read_registry(directory_fd, candidate_id):
    """Read contiguous canonical records, checking candidate and previous SHA."""
    import re
    entries = os.listdir(directory_fd)
    names = []
    for name in entries:
        if re.fullmatch(r'[0-9]{20}\.json', name):
            names.append(name)
        elif name == '.audit-reserve':
            from api.display_bootstrap_audit_reserve import inspect
            reserve = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=directory_fd)
            try:
                inspect(reserve)
            finally:
                os.close(reserve)
        elif name != 'evidence' and re.fullmatch(r'\.tmp-[0-9a-f]{32}', name) is None:
            raise ValueError('STATE_CONFLICT')
    names.sort()
    result = []
    previous = None
    for seq, name in enumerate(names, 1):
        if name != f'{seq:020d}.json':
            raise ValueError('STATE_CONFLICT')
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        try:
            import stat
            identity = os.fstat(fd)
            if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1:
                raise ValueError('IDENTITY_CHANGED')
            if (identity.st_uid != os.fstat(directory_fd).st_uid
                    or identity.st_mode & 0o022
                    or any(key.startswith('system.posix_acl_') for key in os.listxattr(fd))):
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
            if not 0 < identity.st_size <= 65536:
                raise ValueError('STATE_CONFLICT')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                raw = stream.read(65537)
            after = os.fstat(fd)
            named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            fields = ('st_dev','st_ino','st_uid','st_gid','st_mode','st_nlink',
                      'st_size','st_mtime_ns','st_ctime_ns')
            if (len(raw) != identity.st_size or any(
                    getattr(identity, key) != getattr(other, key)
                    for key in fields for other in (after, named))):
                raise ValueError('IDENTITY_CHANGED')
        finally:
            os.close(fd)
        try:
            record = validate_record(parse_record(raw), 'registry')
        except ValueError as exc:
            raise ValueError('STATE_CONFLICT') from exc
        if record['seq'] != seq or record['candidate_id'] != candidate_id or record['previous_sha'] != previous:
            raise ValueError('STATE_CONFLICT')
        transitions = {'RESERVED': ('BUILDING', 'FAILED', 'QUARANTINED'),
                       'BUILDING': ('VERIFIED', 'FAILED', 'QUARANTINED'),
                       'VERIFIED': ('APPROVED', 'FAILED', 'QUARANTINED'),
                       'APPROVED': ('PUBLISH_INTENT', 'FAILED', 'QUARANTINED'),
                       'PUBLISH_INTENT': ('PUBLISHED_UNACTIVATED', 'QUARANTINED'),
                       'PUBLISHED_UNACTIVATED': ('QUARANTINED',),
                       'FAILED': (), 'QUARANTINED': ()}
        if not result:
            if record['state'] != 'RESERVED':
                raise ValueError('STATE_CONFLICT')
        else:
            prior = result[-1]
            if record['state'] not in transitions[prior['state']]:
                raise ValueError('STATE_CONFLICT')
            for key in ('manifest_sha', 'approval_id'):
                if prior[key] is not None and prior[key] != record[key]:
                    raise ValueError('STATE_CONFLICT')
        result.append(record)
        previous = digest(raw)
    return result


def write_record(directory_fd, name, raw, *, reserve_fd=None):
    """Append canonical bytes; optional explicit preallocated audit source.

    A supplied reserve never falls back to fresh allocation on exhaustion.
    Candidate manifest writes must not use the independent audit reserve.
    """
    if reserve_fd is not None:
        from api.display_bootstrap_audit_reserve import write_reserved
        return write_reserved(reserve_fd, directory_fd, name, raw)

    if type(name) is not str or not name or name in ('.', '..') or '/' in name or '\x00' in name:
        raise ValueError('INVALID_INPUT')
    name.encode('utf-8', 'strict')
    parse_record(raw)
    temporary = '.tmp-' + secrets.token_hex(16)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory_fd)
    try:
        remaining = memoryview(raw)
        while remaining:
            written = os.write(fd, remaining)
            if written == 0:
                raise OSError(errno.EIO, 'short record write')
            remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    rename_no_replace(directory_fd, temporary, directory_fd, name)
    os.fsync(directory_fd)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            actual = stream.read(65537)
        if actual != raw:
            raise OSError(errno.EIO, 'record readback differs')
    finally:
        os.close(fd)



def rename_no_replace(source_fd, source_name, target_fd, target_name):
    """Rename relative to already-held directory FDs without overwrite fallback."""
    for name in (source_name, target_name):
        if type(name) is not str or not name or name in ('.', '..') or '/' in name or '\x00' in name:
            raise ValueError('INVALID_INPUT')
        name.encode('utf-8', 'strict')
    if sys.platform != 'linux':
        raise OSError(errno.ENOSYS, 'renameat2 unavailable')
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameat2
    except AttributeError as exc:
        raise OSError(errno.ENOSYS, 'renameat2 unavailable') from exc
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    result = rename(source_fd, os.fsencode(source_name), target_fd, os.fsencode(target_name), 1)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
