"""Protected authority locks; each operation owns a fresh open description."""
import fcntl
import os
import stat
from api.display_bootstrap_manifest import parse_record, canonical_bytes, digest, validate_record
from api.display_bootstrap_v3 import hex_value


FIELDS = frozenset('format_version authority_id host_fs_uuid host_device root_inode '
                   'lock_identity deployment_roots volume_profile_sha'.split())


def validate_authority(value):
    import re
    if (type(value) is not dict or set(value) != FIELDS
            or type(value['format_version']) is not int or value['format_version'] != 1):
        raise ValueError('INVALID_INPUT')
    hex_value(value['authority_id'], 32)
    hex_value(value['volume_profile_sha'], 64)
    if value['host_fs_uuid'] == '00000000-0000-0000-0000-000000000000':
        raise ValueError('INVALID_INPUT')
    if (type(value['host_fs_uuid']) is not str or re.fullmatch(
            '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
            value['host_fs_uuid']) is None):
        raise ValueError('INVALID_INPUT')
    for name, fields in [('host_device', {'major', 'minor'}), ('lock_identity', {'dev', 'ino'})]:
        row = value[name]
        if (type(row) is not dict or set(row) != fields
                or any(type(v) is not int or not 0 <= v <= 9007199254740991 for v in row.values())):
            raise ValueError('INVALID_INPUT')
    if (type(value['root_inode']) is not int or not 1 <= value['root_inode'] <= 9007199254740991
            or value['lock_identity']['ino'] < 1):
        raise ValueError('INVALID_INPUT')
    roots = value['deployment_roots']
    if type(roots) is not dict or set(roots) != {
            'candidate_root', 'publish_root', 'registry_root', 'approval_root', 'ledger_root'}:
        raise ValueError('INVALID_INPUT')
    for identity in roots.values():
        validate_record(identity, 'directory_identity')
    if len(canonical_bytes(value)) > 16384:
        raise ValueError('INVALID_INPUT')
    return value


def _protected(fd, mode, *, size=None):
    info = os.fstat(fd)
    if (info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) != mode
            or any(k.startswith('system.posix_acl_') for k in os.listxattr(fd))):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    if size is not None and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != size):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    return info


class AuthorityLock:
    """Root launcher only. No initialization, rewriting, or LOCK_UN on close."""
    def __init__(self, host_root_fd, *, authority_id, authority_sha, volume_profile_sha):
        self.fd = None
        self._pid = os.getpid()
        directory = record = None
        try:
            if os.geteuid() != 0:
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
            root = os.fstat(host_root_fd)
            from api.display_bootstrap_storage_topology import mount_for_fd
            mount = mount_for_fd(host_root_fd)
            if not stat.S_ISDIR(root.st_mode):
                raise ValueError('UNSUPPORTED_PLATFORM')
            # mountinfo.root='/' alone also matches subdirectories of that mount.
            # Compare the held inode with the actual mount point, not a path claim.
            mount_fd = os.open(mount['target'], os.O_RDONLY | os.O_DIRECTORY
                               | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                mounted = os.fstat(mount_fd)
                if ((mounted.st_dev, mounted.st_ino) != (root.st_dev, root.st_ino)
                        or mount_for_fd(mount_fd)['mount_id'] != mount['mount_id']):
                    raise ValueError('IDENTITY_CHANGED')
            finally:
                os.close(mount_fd)
            directory = os.open('.hermes-bootstrap-budget', os.O_RDONLY | os.O_DIRECTORY
                                | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=host_root_fd)
            di = _protected(directory, 0o700)
            if di.st_dev != root.st_dev:
                raise ValueError('IDENTITY_CHANGED')
            record = os.open('authority.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                             | os.O_NONBLOCK, dir_fd=directory)
            ri = os.fstat(record)
            _protected(record, 0o600, size=ri.st_size)
            if not 0 < ri.st_size <= 16384:
                raise ValueError('INVALID_INPUT')
            raw = os.pread(record, 16385, 0)
            value = validate_authority(parse_record(raw))
            if (digest(raw) != authority_sha or value['authority_id'] != authority_id
                    or value['volume_profile_sha'] != volume_profile_sha
                    or value['root_inode'] != root.st_ino
                    or value['host_device'] != dict(major=os.major(root.st_dev), minor=os.minor(root.st_dev))):
                raise ValueError('IDENTITY_CHANGED')
            self.fd = os.open('lock', os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
                              | os.O_NONBLOCK, dir_fd=directory)
            info = _protected(self.fd, 0o600, size=4096)
            if value['lock_identity'] != dict(dev=info.st_dev, ino=info.st_ino) or info.st_dev != root.st_dev:
                raise ValueError('IDENTITY_CHANGED')
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError('LOCK_BUSY') from exc
            # Re-read the protected record while holding the lock.
            after = os.fstat(record)
            if (os.pread(record, 16385, 0) != raw or any(getattr(after, k) != getattr(ri, k)
                    for k in ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid',
                              'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))):
                raise ValueError('IDENTITY_CHANGED')
            self.authority = value
            self.identity = (info.st_dev, info.st_ino)
        except BaseException:
            self.close()
            raise
        finally:
            if record is not None:
                os.close(record)
            if directory is not None:
                os.close(directory)

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
