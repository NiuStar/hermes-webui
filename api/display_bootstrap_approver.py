"""External approval durable write. Caller owns verified role, policy and lock.

No creator context is accepted or issued. Never creates approval from inferred
intent. A failure after rename is explicitly uncertain and retains all files.
"""
import os
import secrets

from api.display_bootstrap_audit_log import _deadline
from api.display_bootstrap_approval_budget import require_new_record, measure, validate_policy
from api.display_bootstrap_manifest import canonical_bytes, validate_record, digest
from api.display_bootstrap_publish import rename_no_replace
from api.display_bootstrap_roles import verify_approval_root


class ApprovalUncertain(OSError):
    """Final name may exist; do not retry blindly or report no approval."""


def write_approval(root_fd, record, config, policy, *, deadline):
    validate_policy(policy)
    validate_record(config, 'deployment')
    validate_record(record, 'approval')
    if (config['format_version'] != 3 or record['format_version'] != 2
            or record['actor']['profile_id'] != policy['hard_limit_profile_id']
            or digest(canonical_bytes(config)) != record['policy_sha']
            or policy['approval_policy_id'] != config['approval_policy_id']
            or digest(canonical_bytes(policy)) != config['approval_policy_sha']
            or record['candidate_id'] != record['target_name']
            or not record['approver_reference'].strip()
            or any(policy[k] != config[k] for k in
                   ('approver_uid', 'approver_gid', 'approval_read_gid'))):
        raise ValueError('APPROVAL_MISMATCH')
    identity = verify_approval_root(root_fd, config, role='approver', before_confinement=True)
    if identity != policy['approval_root_identity']:
        raise ValueError('IDENTITY_CHANGED')
    raw = canonical_bytes(record)
    require_new_record(root_fd, policy, len(raw), deadline)
    final = record['approval_id'] + '.json'
    temporary = '.' + record['approval_id'] + '.' + secrets.token_hex(16) + '.pending'
    fd = None
    rename_attempted = False
    try:
        _deadline(deadline)
        fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                     | os.O_CLOEXEC, 0o640, dir_fd=root_fd)
        os.fchmod(fd, 0o640)
        before = os.fstat(fd)
        if before.st_uid != config['approver_uid'] or before.st_gid != config['approval_read_gid']:
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        cursor = 0
        while cursor < len(raw):
            _deadline(deadline)
            count = os.write(fd, raw[cursor:])
            if not 0 < count <= len(raw) - cursor:
                raise OSError('short approval write')
            cursor += count
        os.fsync(fd)
        if os.pread(fd, len(raw) + 1, 0) != raw:
            raise ValueError('IO_FAILURE')
        current = measure(root_fd, policy, deadline)
        if current['allocated_bytes'] > policy['max_total_allocated_bytes']:
            raise ValueError('RESOURCE_LIMIT')
        verify_approval_root(root_fd, config, role='approver', before_confinement=True)
        named = os.stat(temporary, dir_fd=root_fd, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError('IDENTITY_CHANGED')
        _deadline(deadline)
        rename_attempted = True
        rename_no_replace(root_fd, temporary, root_fd, final)
        os.fsync(root_fd)
        named = os.stat(final, dir_fd=root_fd, follow_symlinks=False)
        held = os.fstat(fd)
        if ((named.st_dev, named.st_ino, named.st_size, named.st_nlink)
                != (held.st_dev, held.st_ino, len(raw), 1)
                or os.pread(fd, len(raw) + 1, 0) != raw):
            raise ValueError('IDENTITY_CHANGED')
        _deadline(deadline)
        return dict(approval_id=record['approval_id'], approval_sha=digest(raw),
                    status='APPROVAL_DURABLE')
    except (OSError, ValueError) as exc:
        if rename_attempted:
            raise ApprovalUncertain('approval final name requires durable readback') from exc
        raise
    finally:
        if fd is not None:
            os.close(fd)
