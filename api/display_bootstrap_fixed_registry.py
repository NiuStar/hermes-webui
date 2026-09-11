"""Fixed-log lifecycle operations. No context issuance or confinement bypass."""
import os
from contextlib import contextmanager

from api.display_bootstrap_audit_codec_v2 import decode_header, HEADER_BYTES
from api.display_bootstrap_audit_log import AuditFile, _deadline
from api.display_bootstrap_audit_budget import require_slots, validate_layout
from api.display_bootstrap_extents import verify_allocated
from api.display_bootstrap_manifest import canonical_bytes, parse_record, digest, validate_record


def actor_for(ctx):
    from api.display_bootstrap_runner_binding import resolve_runner_binding
    ctx.check_owner()
    return resolve_runner_binding(ctx.config, ctx.resource, ctx.role).actor()


def envelope_bytes(record, actor):
    from api.display_bootstrap_v3 import validate_actor
    validate_actor(actor)
    if len(canonical_bytes(record)) > 60000:
        raise ValueError('INVALID_INPUT')
    return canonical_bytes(dict(format_version=2, actor=actor, record=record))


@contextmanager
def open_registry(ctx, directory_fd, candidate_id, *, writable=False):
    ctx.check_owner()
    if ctx.config.get("format_version") != 3 or ctx.resource.get("format_version") != 3:
        raise ValueError("ACCESS_BOUNDARY_UNPROVEN")
    layout = validate_layout(ctx.resource)
    deadline = ctx.operation_deadline()
    if writable:
        # Flags alone never authorize a write. Context must revalidate the live
        # boundary and its candidate binding before this adapter opens the FD.
        ctx.verify_audit_boundary(directory_fd, candidate_id)
    fd = os.open('audit.bin', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                 dir_fd=directory_fd)
    try:
        _deadline(deadline)
        header = decode_header(os.pread(fd, HEADER_BYTES, 0))
        held = os.fstat(fd)
        if ({k: getattr(held, 'st_' + k) for k in header['file_identity']}
                != header['file_identity']):
            raise ValueError('IDENTITY_CHANGED')
        if (header['candidate_id'] != candidate_id
                or header['deployment_sha'] != ctx.deployment_sha
                or header['resource_sha'] != ctx.config['resource_policy_sha']
                or digest(canonical_bytes(ctx.resource)) != header['resource_sha']
                or header['slot_count'] != layout['slot_count']):
            raise ValueError('APPROVAL_MISMATCH')
    finally:
        os.close(fd)
    with AuditFile(directory_fd, header, deadline=deadline,
                   verify_allocation=verify_allocated, writable=writable, resource=ctx.resource) as log:
        log.actor = actor_for(ctx)
        # Never interpret a committed-looking record until durable confirmation.
        log.confirm_durable()
        yield log


def append_state(log, state, *, manifest_sha=None, approval_id=None, error_code=None):
    before = log.inspect()
    prior = before['registry_last']
    if prior is None:
        raise ValueError('STATE_CONFLICT')
    failure = state in ('FAILED', 'QUARANTINED')
    require_slots(before, operation='failure' if failure else 'advance')
    for field, supplied in [('manifest_sha', manifest_sha), ('approval_id', approval_id)]:
        if supplied is not None and prior[field] is not None and supplied != prior[field]:
            raise ValueError('APPROVAL_MISMATCH')
    value = dict(format_version=1, candidate_id=prior['candidate_id'],
                 target_name=prior['target_name'], seq=prior['seq'] + 1,
                 previous_sha=before['registry_sha'], state=state,
                 manifest_sha=prior['manifest_sha'] or manifest_sha,
                 approval_id=prior['approval_id'] or approval_id, error_code=error_code)
    if prior['format_version'] == 2:
        value.update(format_version=2, request_sha=prior['request_sha'])
    validate_record(value, 'registry')
    receipt = log.append(1, envelope_bytes(value, log.actor))
    after = log.inspect()
    if after['registry_last'] != value or after['registry_sha'] != receipt['logical_sha']:
        raise ValueError('AUDIT_UNAVAILABLE')
    return value


def persist_platform(log, evidence, deployment_sha):
    validate_record(evidence, 'platform_evidence')
    if evidence['approved_policy_sha'] != deployment_sha:
        raise ValueError('APPROVAL_MISMATCH')
    before = log.inspect()
    if before['registry_last'] is None or before['registry_last']['state'] != 'BUILDING':
        raise ValueError('STATE_CONFLICT')
    require_slots(before, operation='advance')
    raw = envelope_bytes(evidence, log.actor)
    receipt = log.append(2, raw)
    if log.get(2, receipt['logical_key']) != raw:
        raise ValueError('AUDIT_UNAVAILABLE')
    return receipt['logical_sha']


def platform_evidence(log, sha, deployment_sha):
    raw = log.get(2, sha)
    if raw is None or digest(raw) != sha:
        raise ValueError('AUDIT_UNAVAILABLE')
    envelope = parse_record(raw)
    from api.display_bootstrap_audit_codec_v2 import bind_actor
    bind_actor(envelope, log._resource)
    if envelope['actor']['role'] != 'creator':
        raise ValueError('APPROVAL_MISMATCH')
    value = validate_record(envelope['record'], 'platform_evidence')
    if value['approved_policy_sha'] != deployment_sha:
        raise ValueError('APPROVAL_MISMATCH')
    return value


def persist_failure(log, code, *, operation, stage, quarantine=False):
    before = log.inspect()
    require_slots(before, operation='failure')
    prior = before['registry_last']
    pending = before['pending_failure']
    if pending is None:
        evidence = dict(format_version=1, candidate_id=prior['candidate_id'],
                        error_code=code, operation=operation, stage=stage,
                        registry_seq=prior['seq'], detail=code)
        log.append(3, envelope_bytes(evidence, log.actor))
    elif pending['error_code'] != code:
        raise ValueError('STATE_CONFLICT')
    return append_state(log, 'QUARANTINED' if quarantine else 'FAILED', error_code=code)
