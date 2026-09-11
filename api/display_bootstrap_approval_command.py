"""Explicit external approval under the global lock, never creator authority."""
import argparse
import os
import time

from api.display_bootstrap_manifest import canonical_bytes, parse_record, validate_record
from api.display_bootstrap_policy import (
    BootstrapRejected, open_protected_root, read_protected_record,
    acquire_fixed_lock, read_active_policy, verify_anchor_binding, directory_identity,
)
from api.display_bootstrap_approver import write_approval, ApprovalUncertain
from api.display_bootstrap_approval_budget import validate_policy
from api.display_bootstrap_roles import verify_dac_configuration
from api.display_bootstrap_storage import _read
from api.display_bootstrap_storage_contract import _hex
from api.display_bootstrap_runner import read_hard_limit_profile, verify_systemd_runner
from api.display_bootstrap_audit_log import _deadline


def issue(policy_id, record):
    started = time.monotonic()
    _hex(policy_id, 32)
    validate_record(record, 'approval')
    root = approval = None
    lock = None
    try:
        root = open_protected_root('/etc/hermes-display-bootstrap', {0})
        anchor = validate_record(parse_record(read_protected_record(root, 'anchor.json', {0})), 'anchor')
        lock = acquire_fixed_lock(root, anchor['lock_identity'])
        config, sha = read_active_policy(root, policy_id)
        if config['format_version'] != 3 or record['policy_sha'] != sha:
            raise BootstrapRejected('APPROVAL_MISMATCH')
        verify_anchor_binding(root, anchor, config)
        policy, policy_sha = _read('/etc/hermes-display-bootstrap/approval-policies', config['approval_policy_id'])
        validate_policy(policy)
        if policy_sha != config['approval_policy_sha'] or policy['approval_policy_id'] != config['approval_policy_id']:
            raise BootstrapRejected('APPROVAL_MISMATCH')
        from api.display_bootstrap_policy import read_resource_policy
        from api.display_bootstrap_runner_binding import resolve_runner_binding, verify_runner_binding
        resource = read_resource_policy(root, config)
        binding = resolve_runner_binding(config, resource, 'approver')
        if policy['hard_limit_profile_id'] != binding.profile_id:
            raise BootstrapRejected('APPROVAL_MISMATCH')
        deadline = started + resource['max_elapsed_seconds']
        verify_runner_binding(binding, config, resource, deadline)
        record = dict(record, format_version=2, actor=binding.actor())
        validate_record(record, 'approval')
        approval = open_protected_root(config['roots']['approval_root']['path'], {0, config['approver_uid']})
        roots = {'approval_root': approval, 'lock_root': root}
        verify_dac_configuration(roots, config, role='approver', before_confinement=True)
        if read_active_policy(root, policy_id) != (config, sha):
            raise BootstrapRejected('APPROVAL_MISMATCH')
        if _read('/etc/hermes-display-bootstrap/approval-policies', config['approval_policy_id']) != (policy, policy_sha):
            raise BootstrapRejected('APPROVAL_MISMATCH')
        fresh = open_protected_root(config['roots']['approval_root']['path'], {0, config['approver_uid']})
        try:
            if directory_identity(fresh) != directory_identity(approval):
                raise BootstrapRejected('IDENTITY_CHANGED')
        finally:
            os.close(fresh)
        _deadline(deadline)
        if read_resource_policy(root, config) != resource:
            raise BootstrapRejected('APPROVAL_MISMATCH')
        verify_runner_binding(binding, config, resource, deadline)
        return write_approval(approval, record, config, policy, deadline=deadline)
    finally:
        try:
            if approval is not None:
                os.close(approval)
        finally:
            try:
                if lock is not None:
                    lock.close()
            finally:
                if root is not None:
                    os.close(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Explicit external offline approval')
    for field in ('policy-id', 'candidate-id', 'approval-id', 'manifest-sha', 'policy-sha', 'reference'):
        parser.add_argument('--' + field, required=True)
    args = parser.parse_args(argv)
    try:
        for value in (args.policy_id, args.candidate_id, args.approval_id):
            _hex(value, 32)
        for value in (args.manifest_sha, args.policy_sha):
            _hex(value, 64)
        record = dict(format_version=1, approval_id=args.approval_id,
                      candidate_id=args.candidate_id, target_name=args.candidate_id,
                      manifest_sha=args.manifest_sha, policy_sha=args.policy_sha,
                      scope='PUBLISH_EMPTY_UNACTIVATED',
                      approver_reference=args.reference)
        validate_record(record, 'approval')
        if not args.reference.strip():
            raise ValueError('INVALID_INPUT')
    except ValueError:
        parser.error('invalid identifier, digest, or explicit approval reference')
    try:
        result = issue(args.policy_id, record)
        exit_code = 0
    except ApprovalUncertain:
        result = dict(status='UNCERTAIN', code='IO_FAILURE', approval_id=args.approval_id)
        exit_code = 1
    except (OSError, ValueError) as exc:
        allowed = {'INVALID_INPUT', 'POLICY_MISSING', 'APPROVAL_MISMATCH', 'IDENTITY_CHANGED',
                   'ACCESS_BOUNDARY_UNPROVEN', 'RESOURCE_LIMIT', 'LOCK_BUSY', 'UNSUPPORTED_PLATFORM'}
        code = str(exc) if str(exc) in allowed else 'IO_FAILURE'
        result = dict(status='BLOCKED', code=code, approval_id=args.approval_id)
        exit_code = 1
    import sys
    sys.stdout.buffer.write(canonical_bytes(result) + b'\n')
    sys.stdout.buffer.flush()
    return exit_code
