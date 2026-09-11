"""Only internal two-stage entrypoint may issue candidate-scoped contexts."""
import os
import time
from api.display_bootstrap_policy import (BootstrapRejected, BootstrapContext, open_protected_root,
    read_protected_record, acquire_fixed_lock, read_active_policy, read_resource_policy,
    verify_anchor_binding, open_policy_roots)
from api.display_bootstrap_manifest import parse_record, validate_record
from api.display_bootstrap_admission_ownership import _new_preparation, _register_receipt, _consume_receipt
from api.display_bootstrap_admission_checks import install, verify_prepared


def _prepare(policy_id, role, started):
    from api.display_bootstrap_roles import verify_identity, verify_dac_configuration
    from api.display_bootstrap_runner_binding import resolve_runner_binding, verify_runner_binding
    from api.display_bootstrap_storage_binding import hold_storage_binding
    from api.display_bootstrap_worker_release import hold_worker_release
    from api.display_bootstrap_storage_client import request
    from api.display_bootstrap_volume import verify_fixed_volumes
    # The finite bootstrap lookup budget is never added to the operation budget.
    p = _new_preparation(started+15)
    try:
        p.started, p.role = started, role
        p.root_fd = open_protected_root('/etc/hermes-display-bootstrap',{0})
        p.owner.callback(os.close,p.root_fd)
        anchor = validate_record(parse_record(read_protected_record(p.root_fd,'anchor.json',{0})),'anchor')
        p.lock = acquire_fixed_lock(p.root_fd,anchor['lock_identity'])
        p.owner.callback(p.lock.close)
        p.config,p.deployment_sha = read_active_policy(p.root_fd,policy_id)
        verify_anchor_binding(p.root_fd,anchor,p.config)
        if p.config['format_version'] != 3:
            raise ValueError('APPROVAL_MISMATCH')
        p.resource = read_resource_policy(p.root_fd,p.config)
        p.deadline = started+p.resource['max_elapsed_seconds']
        from api.display_bootstrap_audit_log import _deadline
        _deadline(p.deadline)
        verify_identity(p.config,role)
        p.roots = open_policy_roots(p.config)
        for fd in p.roots.values():
            p.owner.callback(os.close,fd)
        verify_dac_configuration(p.roots,p.config,role=role,before_confinement=True)
        from api.display_bootstrap_policy import verify_platform
        import secrets
        p.platform_evidence = dict(verify_platform(p.roots['candidate_root'],p.config['platform_requirements']),
                                  format_version=1,evidence_id=secrets.token_hex(16),
                                  approved_policy_sha=p.deployment_sha)
        p.runner = resolve_runner_binding(p.config,p.resource,role)
        p.runner_evidence = verify_runner_binding(p.runner,p.config,p.resource,p.deadline)
        p.storage = p.owner.enter_context(hold_storage_binding(policy_id,deadline=p.deadline))
        p.release = p.owner.enter_context(hold_worker_release(deadline=p.deadline))
        report = p.storage.snapshot('report')
        if (p.release.commit != report['code_commit'] or os.uname().release != report['kernel_release']
                or os.readlink('/proc/self/ns/mnt') != p.storage.snapshot('contract')['namespace_id']):
            raise ValueError('APPROVAL_MISMATCH')
        verify_fixed_volumes(p.roots,p.resource,profile_id=p.storage.binding.volume_profile_id)
        p.observation = request(p.storage,role=role,registry_fd=p.roots['registry_root'],deadline=p.deadline)
        p.storage.revalidate()
        return p
    except BaseException:
        p.close()
        raise


def _construct(p, verified, owner):
    if p.mode not in ('CREATE','PUBLISH'):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    value = object.__new__(BootstrapContext)
    value.pid, value.closed, value.role = p.pid, False, p.role
    value.started_monotonic = p.started
    value.root_fd,value.lock,value.roots = p.root_fd,p.lock,p.roots
    value.config,value.resource,value.deployment_sha = p.config,p.resource,p.deployment_sha
    value.storage_handle,value.release_handle = p.storage,p.release
    value.candidate_id,value.audit_fd = p.candidate_id,p.audit_fd
    value.candidate_fd,value.candidate_identity = p.candidate_fd,p.candidate_identity
    value.audit_boundary = p.boundary
    value.creation_confined,value.publication_confined = p.mode=='CREATE',p.mode=='PUBLISH'
    value._admission_owner,value._admission_verified = owner,verified
    value.prepared_audit = p.audit_snapshot
    value.evidence = p.platform_evidence
    value.storage_observation = p.observation
    return value


def issue(receipt):
    return _consume_receipt(receipt,verify_prepared,_construct)


def execute_operation(policy_id, operation, candidate_id=None, approval_id=None):
    from api.display_bootstrap_v3 import hex_value
    from api.display_bootstrap_lifecycle import _blocked, _publication_error_code
    from api.display_bootstrap_preparation_steps import prepare_create, prepare_existing
    started = time.monotonic()
    p = None
    try:
        hex_value(policy_id,32)
        if operation not in ('create','publish','recover'):
            raise ValueError('INVALID_INPUT')
        if operation == 'create' and approval_id is not None:
            raise ValueError('INVALID_INPUT')
        if operation != 'create' or candidate_id is not None:
            hex_value(candidate_id,32)
        if operation == 'publish':
            hex_value(approval_id,32)
        elif approval_id is not None:
            raise ValueError('INVALID_INPUT')
        role = dict(create='creator',publish='publisher',recover='recover')[operation]
        p = _prepare(policy_id,role,started)
        if operation == 'create':
            if candidate_id is not None:
                p.requested_candidate_id = candidate_id
            prepare_create(p)
        else:
            prepare_existing(p,candidate_id,approval_id)
        candidate_id = p.candidate_id
        if p.mode == 'TERMINAL':
            last = p.audit_snapshot['registry_last']
            result = _blocked('STATE_CONFLICT',candidate_id,last['seq'])
            if last['state'] == 'QUARANTINED':
                result.update(status='QUARANTINED',quarantine_persisted=True)
            return result
        install(p)
        verified = verify_prepared(p)
        receipt = _register_receipt(p,verified)
        if p.mode == 'AUDIT_TERMINATE':
            return _consume_receipt(receipt,verify_prepared,_terminate)
        with BootstrapContext._issue(receipt) as ctx:
            from api.display_bootstrap_fixed_lifecycle import create, publish
            return create(ctx) if operation=='create' else publish(ctx,candidate_id,approval_id)
    except (OSError,ValueError) as exc:
        if p is not None:
            candidate_id = getattr(p,'candidate_id',candidate_id)
        return _blocked(_publication_error_code(exc),candidate_id)
    finally:
        if p is not None:
            p.close()


def _terminate(p, verified, owner):
    from api.display_bootstrap_audit_log import AuditFile
    from api.display_bootstrap_extents import verify_allocated
    from api.display_bootstrap_fixed_registry import persist_failure
    from api.display_bootstrap_lifecycle import _blocked
    from api.display_bootstrap_audit_codec_v2 import decode_header, HEADER_BYTES
    try:
        if p.mode != 'AUDIT_TERMINATE' or p.role != 'recover':
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        fd = os.open('audit.bin',os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=p.audit_fd)
        try:
            header = decode_header(os.pread(fd,HEADER_BYTES,0))
        finally:
            os.close(fd)
        with AuditFile(p.audit_fd,header,deadline=p.deadline,verify_allocation=verify_allocated,
                       writable=True,resource=p.resource) as log:
            log.actor = p.runner.actor()
            log.confirm_durable()
            if log.inspect() != p.audit_snapshot:
                raise ValueError('IDENTITY_CHANGED')
            last = log.inspect()['registry_last']
            if last['state'] not in ('RESERVED','BUILDING'):
                raise ValueError('STATE_CONFLICT')
            failed = persist_failure(log,'IO_FAILURE',operation='recover',stage='RECOVER')
            return _blocked('IO_FAILURE',p.candidate_id,failed['seq'])
    finally:
        owner.close()
