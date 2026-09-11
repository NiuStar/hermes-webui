"""Fixed preparation steps; preparation is never a BootstrapContext."""
import os
import secrets
from api.display_bootstrap_admission_ownership import _check_preparation
from api.display_bootstrap_policy import directory_identity, read_protected_record
from api.display_bootstrap_manifest import parse_record, validate_record, canonical_bytes, digest
from api.display_bootstrap_audit_log import prepare
from api.display_bootstrap_audit_budget import validate_layout
from api.display_bootstrap_extents import verify_allocated
from api.display_bootstrap_prepared_audit import scan_prepared_audit
from api.display_bootstrap_quota import _candidate_quota_transport


def _directory(p, parent, name, *, create=False):
    _check_preparation(p)
    if create:
        from api.display_bootstrap_growth_gate import before_growth
        before_growth(p)
        os.mkdir(name, 0o700, dir_fd=parent)
        os.fsync(parent)
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    p.owner.callback(os.close, fd)
    identity = directory_identity(fd)
    if identity['uid'] != p.config['creator_uid'] or identity['mode'] & 0o077:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    return fd


def _quota_prepare(p, *, allocate=False):
    _check_preparation(p)
    if allocate and (p.role != 'creator' or p.mode != 'CREATE'):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    if directory_identity(p.candidate_fd) != p.candidate_identity:
        raise ValueError('IDENTITY_CHANGED')
    return _candidate_quota_transport(config=p.config, resource=p.resource, role=p.role,
        deployment_sha=p.deployment_sha, deadline=p.deadline, cid=p.candidate_id,
        fd=p.candidate_fd, allocate=allocate)


def _scan_audit(p):
    _check_preparation(p)
    with scan_prepared_audit(directory_fd=p.audit_fd, candidate_id=p.candidate_id,
            deployment_sha=p.deployment_sha, resource=p.resource, actor=p.runner.actor(),
            deadline=p.deadline) as log:
        return log.inspect()


def prepare_create(p):
    _check_preparation(p)
    if p.role != 'creator':
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    from pathlib import Path
    from api.display_bootstrap_audit_log import _deadline
    policy = p.resource
    rss = int(Path('/proc/self/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
    candidate = os.fstatvfs(p.roots['candidate_root'])
    audit = os.fstatvfs(p.roots['registry_root'])
    if (rss > policy['max_rss_bytes']
            or candidate.f_bavail*candidate.f_frsize < policy['min_free_bytes']+policy['max_candidate_bytes']
            or audit.f_bavail*audit.f_frsize < policy['min_free_bytes']+policy['audit_reserve_bytes']
            or candidate.f_favail < 2 or audit.f_favail < 2):
        raise ValueError('RESOURCE_LIMIT')
    retained = set()
    for root in ('candidate_root','publish_root','registry_root'):
        with os.scandir(p.roots[root]) as entries:
            for entry in entries:
                _deadline(p.deadline)
                retained.add(entry.name)
                if len(retained) >= policy['max_retained_candidates']:
                    raise ValueError('RESOURCE_LIMIT')
    requested = getattr(p, 'requested_candidate_id', None)
    if p.resource['format_version'] == 4 and requested is None:
        raise ValueError('INVALID_INPUT')
    if requested is not None:
        from api.display_bootstrap_v3 import hex_value
        hex_value(requested, 32)
    p.mode, p.candidate_id = 'CREATE', requested if requested is not None else secrets.token_hex(16)
    first = dict(format_version=1, candidate_id=p.candidate_id, target_name=p.candidate_id,
                 seq=1, previous_sha=None, state='RESERVED', manifest_sha=None,
                 approval_id=None, error_code=None)
    if p.resource['format_version'] == 4:
        p.request_sha = digest(canonical_bytes(dict(format_version=1, operation='create',
            operation_id=p.candidate_id, policy_id=p.config['policy_id'],
            deployment_sha=p.deployment_sha, resource_sha=p.config['resource_policy_sha'])))
        first.update(format_version=2, request_sha=p.request_sha)
    from api.display_bootstrap_fixed_registry import envelope_bytes
    layout = validate_layout(p.resource)
    from api.display_bootstrap_growth_gate import before_growth
    before_growth(p)
    p.prepared = prepare(p.roots['registry_root'], p.candidate_id, p.deployment_sha,
        p.config['resource_policy_sha'], layout['slot_count'], envelope_bytes(first,p.runner.actor()),
        deadline=p.deadline, verify_allocation=verify_allocated, resource=p.resource)
    p.audit_fd = _directory(p,p.roots['registry_root'],p.candidate_id)
    p.candidate_fd = _directory(p,p.roots['candidate_root'],p.candidate_id,create=True)
    p.candidate_identity = directory_identity(p.candidate_fd)
    _quota_prepare(p,allocate=True)
    p.quota = _quota_prepare(p)
    p.audit_snapshot = _scan_audit(p)


def prepare_existing(p, candidate_id, approval_id):
    _check_preparation(p)
    from api.display_bootstrap_v3 import hex_value
    hex_value(candidate_id,32)
    p.candidate_id = candidate_id
    p.audit_fd = _directory(p,p.roots['registry_root'],candidate_id)
    p.audit_snapshot = _scan_audit(p)
    last = p.audit_snapshot['registry_last']
    if last is None:
        raise ValueError('STATE_CONFLICT')
    if last['state'] in ('FAILED','QUARANTINED'):
        p.mode = 'TERMINAL'
        return
    if last['state'] in ('RESERVED','BUILDING'):
        if p.role != 'recover':
            raise ValueError('STATE_CONFLICT')
        p.mode = 'AUDIT_TERMINATE'
        return
    p.mode = 'PUBLISH'
    selected = approval_id if p.role == 'publisher' else last['approval_id']
    hex_value(selected,32)
    if last['approval_id'] not in (None,selected):
        raise ValueError('APPROVAL_MISMATCH')
    raw = read_protected_record(p.roots['approval_root'],selected+'.json',{p.config['approver_uid']})
    record = validate_record(parse_record(raw),'approval')
    from api.display_bootstrap_runner_binding import resolve_runner_binding
    if (record['format_version'] != 2 or record['actor'] != resolve_runner_binding(
            p.config,p.resource,'approver').actor() or any(record[k] != v for k,v in
            dict(approval_id=selected,candidate_id=candidate_id,target_name=candidate_id,
                 manifest_sha=last['manifest_sha'],policy_sha=p.deployment_sha).items())):
        raise ValueError('APPROVAL_MISMATCH')
    p.approval_id, p.approval_raw = selected, raw
    present = []
    for name in ('candidate_root','publish_root'):
        try:
            os.stat(candidate_id,dir_fd=p.roots[name],follow_symlinks=False)
            present.append(name)
        except FileNotFoundError:
            pass
    if len(present) != 1:
        raise ValueError('STATE_CONFLICT')
    source = present[0] == 'candidate_root'
    if (last['state'] == 'PUBLISHED_UNACTIVATED' and source
            or last['state'] in ('VERIFIED','APPROVED') and not source):
        raise ValueError('STATE_CONFLICT')
    p.candidate_fd = _directory(p,p.roots[present[0]],candidate_id)
    p.candidate_identity = directory_identity(p.candidate_fd)
    manifest_raw = read_protected_record(p.candidate_fd,'manifest.json',{p.config['creator_uid']})
    manifest = validate_record(parse_record(manifest_raw),'manifest')
    if (manifest['format_version'] != 3 or digest(manifest_raw) != last['manifest_sha']
            or manifest['candidate_id'] != candidate_id
            or manifest['candidate_directory_identity'] != p.candidate_identity
            or manifest['resource_policy_sha'] != p.config['resource_policy_sha']
            or manifest['audit_header_sha'] != p.audit_snapshot['header_sha']):
        raise ValueError('APPROVAL_MISMATCH')
    p.quota = _quota_prepare(p)
