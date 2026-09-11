"""Non-mutating WRITE_FILE probe; does not prove other Landlock rights."""
import errno
import hashlib
import os
import stat

from api.display_bootstrap_audit_log import _deadline
from api.display_bootstrap_manifest import validate_record


def probe(config, *, denied, deadline):
    from api.display_bootstrap_policy import open_protected_root
    if type(denied) is not bool:
        raise ValueError('INVALID_INPUT')
    expected = validate_record(config['boundary_probe_identity'], 'file_identity')
    if (type(config['creator_uid']) is not int or config['creator_uid'] <= 0
            or expected['uid'] != config['creator_uid']
            or stat.S_IMODE(expected['mode']) != 0o600):
        raise ValueError('INVALID_INPUT')
    import re
    sha = config['boundary_probe_sha']
    if type(sha) is not str or re.fullmatch('[0-9a-f]{64}', sha) is None:
        raise ValueError('INVALID_INPUT')
    directory = open_protected_root('/var/lib/hermes-display-bootstrap/probes/'
                                    + str(config['creator_uid']), {0})
    read_fd = write_fd = None
    try:
        _deadline(deadline)
        read_fd = os.open('denied-write', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                          dir_fd=directory)
        before = os.fstat(read_fd)
        if ({k: getattr(before, 'st_' + k) for k in expected} != expected
                or not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= 4096
                or os.listxattr(read_fd)):
            raise ValueError('IDENTITY_CHANGED')
        if not os.fstatvfs(read_fd).f_flag & os.ST_NOATIME:
            raise ValueError('UNSUPPORTED_PLATFORM')
        raw = os.pread(read_fd, 4097, 0)
        if len(raw) != before.st_size or hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError('IDENTITY_CHANGED')
        rejected = False
        try:
            write_fd = os.open('denied-write', os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                               | os.O_NONBLOCK, dir_fd=directory)
            opened = os.fstat(write_fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError('IDENTITY_CHANGED')
        except OSError as exc:
            if exc.errno != errno.EACCES:
                raise
            rejected = True
        finally:
            if write_fd is not None:
                fd, write_fd = write_fd, None
                os.close(fd)
        after = os.fstat(read_fd)
        named = os.stat('denied-write', dir_fd=directory, follow_symlinks=False)
        fields = ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode', 'st_nlink',
                  'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if (any(getattr(before, k) != getattr(other, k)
                for k in fields for other in (after, named))
                or os.pread(read_fd, 4097, 0) != raw):
            raise ValueError('IDENTITY_CHANGED')
        if rejected != denied:
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        _deadline(deadline)
        return dict(write_file_denied=rejected, identity=expected, sha=sha)
    finally:
        try:
            if read_fd is not None:
                os.close(read_fd)
        finally:
            os.close(directory)
