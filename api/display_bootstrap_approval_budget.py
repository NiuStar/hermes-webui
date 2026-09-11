"""Bounded approval accounting. This is admission accounting, not a quota."""
import os
import re
import stat

from api.display_bootstrap_audit_log import _deadline
from api.display_bootstrap_manifest import canonical_bytes, validate_record


def validate_policy(value):
    fields = {'format_version', 'approval_policy_id', 'approver_uid', 'approver_gid',
              'approval_read_gid', 'approval_root_identity', 'max_record_bytes',
              'max_retained_records', 'min_free_bytes', 'max_total_allocated_bytes',
              'hard_limit_profile_id'}
    if type(value) is not dict or set(value) != fields:
        raise ValueError('INVALID_INPUT')
    canonical_bytes(value)
    for key in fields - {'approval_policy_id', 'approval_root_identity', 'hard_limit_profile_id'}:
        if type(value[key]) is not int or not 0 < value[key] <= 9007199254740991:
            raise ValueError('INVALID_INPUT')
    for key in ('approval_policy_id', 'hard_limit_profile_id'):
        if type(value[key]) is not str or re.fullmatch('[0-9a-f]{32}', value[key]) is None:
            raise ValueError('INVALID_INPUT')
    if (value['format_version'] != 1 or value['max_record_bytes'] > 65536
            or value['max_retained_records'] > 65536
            or value['approver_gid'] == value['approval_read_gid']):
        raise ValueError('INVALID_INPUT')
    validate_record(value['approval_root_identity'], 'directory_identity')
    return value


def measure(fd, policy, deadline):
    validate_policy(policy)
    _deadline(deadline)
    root = os.fstat(fd)
    if ({k: getattr(root, 'st_' + k) for k in policy['approval_root_identity']}
            != policy['approval_root_identity']):
        raise ValueError('IDENTITY_CHANGED')
    count = logical = allocated = 0
    identities = set()
    with os.scandir(fd) as entries:
        for entry in entries:
            _deadline(deadline)
            count += 1
            if count > policy['max_retained_records']:
                raise ValueError('RESOURCE_LIMIT')
            info = entry.stat(follow_symlinks=False)
            logical += info.st_size
            allocated += info.st_blocks * 512
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != policy['approver_uid']
                    or info.st_gid != policy['approval_read_gid']
                    or stat.S_IMODE(info.st_mode) != 0o640
                    or (info.st_dev, info.st_ino) in identities
                    or re.fullmatch(r'(?:[0-9a-f]{32}\.json|\.[0-9a-f]{32}\.[0-9a-f]{32}\.pending)', entry.name) is None):
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
            identities.add((info.st_dev, info.st_ino))
            child = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                held = os.fstat(child)
                named = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                fields = ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode', 'st_nlink',
                          'st_size', 'st_blocks', 'st_mtime_ns', 'st_ctime_ns')
                if (any(getattr(info, k) != getattr(other, k)
                        for k in fields for other in (held, named))
                        or any(k.startswith('system.posix_acl_') for k in os.listxattr(child))):
                    raise ValueError('IDENTITY_CHANGED')
            finally:
                os.close(child)
            if info.st_size > policy['max_record_bytes']:
                raise ValueError('RESOURCE_LIMIT')
    after = os.fstat(fd)
    if (root.st_mtime_ns, root.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError('IDENTITY_CHANGED')
    _deadline(deadline)
    return dict(records=count, logical_bytes=logical, allocated_bytes=allocated)


def require_new_record(fd, policy, payload_bytes, deadline):
    if type(payload_bytes) is not int or not 0 < payload_bytes <= policy['max_record_bytes']:
        raise ValueError('INVALID_INPUT')
    current = measure(fd, policy, deadline)
    space = os.fstatvfs(fd)
    if space.f_frsize <= 0:
        raise ValueError('UNSUPPORTED_PLATFORM')
    rounded = ((payload_bytes + space.f_frsize - 1) // space.f_frsize) * space.f_frsize
    if (current['records'] >= policy['max_retained_records']
            or current['allocated_bytes'] + rounded > policy['max_total_allocated_bytes']
            or space.f_bavail * space.f_frsize < policy['min_free_bytes'] + rounded
            or space.f_favail < 1):
        raise ValueError('RESOURCE_LIMIT')
    return current
