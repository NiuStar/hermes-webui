"""V2 lifecycle: fixed audit media, explicit confinement, no activation."""
import os
import secrets
import sqlite3

from api import display_bootstrap_artifact as artifact
from api import display_bootstrap_publish as storage
from api import display_bootstrap_fixed_registry as registry
from api.display_bootstrap_audit_log import prepare
from api.display_bootstrap_audit_budget import validate_layout
from api.display_bootstrap_extents import verify_allocated
from api.display_bootstrap_manifest import canonical_bytes, digest, validate_record
from api.display_bootstrap_policy import BootstrapRejected, directory_identity


def create(ctx):
    from api.display_bootstrap_lifecycle import _directory, _creator_commit, _blocked, _publication_error_code
    cid = getattr(ctx, 'candidate_id', None)
    if (getattr(ctx, 'role', None) != 'creator' or not getattr(ctx, 'creation_confined', False)
            or not hasattr(ctx, '_admission_owner')):
        return _blocked('ACCESS_BOUNDARY_UNPROVEN')
    try:
        ctx.revalidate()
        deadline = ctx.operation_deadline()
        commit = _creator_commit(ctx)
        prepared = ctx.prepared_audit
        with _directory(ctx.roots['registry_root'], cid) as audit:
            try:
                ctx.revalidate()
                with _directory(ctx.roots['candidate_root'], cid) as candidate:
                    identity = directory_identity(candidate)
                    if identity != ctx.candidate_identity:
                        raise BootstrapRejected('IDENTITY_CHANGED')
                    ctx.verify_candidate_quota(cid, candidate)
                    with registry.open_registry(ctx, audit, cid, writable=True) as log:
                        registry.append_state(log, 'BUILDING')
                        budget(ctx, deadline, candidate)
                        from api.display_bootstrap_growth_gate import before_growth
                        facts = artifact.build_private_database(candidate,
                            max_bytes=ctx.resource['max_candidate_bytes'], deadline=deadline,
                            before_growth=lambda: before_growth(ctx))
                        ctx.verify_candidate_quota(cid, candidate)
                        ctx.revalidate()
                        evidence_sha = registry.persist_platform(log, ctx.evidence, ctx.deployment_sha)
                        manifest = dict(facts, format_version=3, candidate_id=cid,
                            ddl_sha=artifact.PINNED_DDL_SHA, sqlite_version=sqlite3.sqlite_version,
                            platform_evidence_sha=evidence_sha, resource_policy_sha=ctx.config['resource_policy_sha'],
                            candidate_directory_identity=identity, connection_policy=dict(foreign_keys=1,synchronous=2),
                            creator_commit=commit, audit_format='FIXED_LOG_V2', audit_header_sha=prepared['header_sha'])
                        validate_record(manifest, 'manifest')
                        raw = canonical_bytes(manifest)
                        before_growth(ctx)
                        storage.write_record(candidate, 'manifest.json', raw)
                        ctx.revalidate()
                        ctx.verify_candidate_quota(cid, candidate)
                        budget(ctx, deadline, candidate)
                        named = os.stat(cid, dir_fd=ctx.roots['candidate_root'], follow_symlinks=False)
                        if {k: getattr(named, 'st_'+k) for k in identity} != identity:
                            raise BootstrapRejected('IDENTITY_CHANGED')
                        result = registry.append_state(log, 'VERIFIED', manifest_sha=digest(raw))
                        return dict(format_version=1,status='VERIFIED',code='OK',candidate_id=cid,
                                    target_name=cid,registry_seq=result['seq'],manifest_sha=digest(raw))
            except (OSError, ValueError, sqlite3.Error) as exc:
                code = _publication_error_code(exc)
                # A torn writer cannot be reused. Fresh reader verifies the entire
                # container; failure of confinement leaves only retained evidence.
                if getattr(ctx, 'audit_boundary', None) is not None:
                    try:
                        with registry.open_registry(ctx, audit, cid, writable=True) as log:
                            state = registry.persist_failure(log,code,operation='create',stage='BUILD',
                                quarantine=code in ('IDENTITY_CHANGED','STATE_CONFLICT'))
                            return _blocked(code,cid,state['seq'])
                    except (OSError, ValueError):
                        pass
                return _blocked(code,cid)
    except (OSError, ValueError, sqlite3.Error) as exc:
        return _blocked(_publication_error_code(exc),cid)


def publish(ctx, cid, approval_id):
    from api.display_bootstrap_lifecycle import (
        _directory, _blocked, _publication_error_code, load_approval,
    )
    from api.display_bootstrap_policy import read_protected_record
    from api.display_bootstrap_manifest import parse_record
    from api.display_bootstrap_audit_budget import require_slots
    import stat
    uncertain, seq = False, None
    expected_role = 'publisher' if approval_id is not None else 'recover'
    if getattr(ctx, 'role', None) != expected_role or getattr(ctx, 'creation_confined', False):
        return _blocked('ACCESS_BOUNDARY_UNPROVEN', cid)
    try:
        deadline = ctx.operation_deadline()
        with _directory(ctx.roots['registry_root'], cid) as audit:
            with registry.open_registry(ctx, audit, cid) as reader:
                initial = reader.inspect()
            record = initial['registry_last']
            if record is None:
                return _blocked('STATE_CONFLICT', cid)
            seq = record['seq']
            if record['state'] in ('FAILED', 'QUARANTINED'):
                result = _blocked('STATE_CONFLICT', cid, seq)
                if record['state'] == 'QUARANTINED':
                    result.update(status='QUARANTINED', quarantine_persisted=True)
                return result
            ctx.verify_audit_boundary(audit, cid)
            with registry.open_registry(ctx, audit, cid, writable=True) as log:
                if log.inspect() != initial:
                    raise BootstrapRejected('IDENTITY_CHANGED')
                if record['state'] in ('RESERVED', 'BUILDING'):
                    failed = registry.persist_failure(log, 'IO_FAILURE', operation='publish' if expected_role == 'publisher' else 'recover', stage='RECOVER')
                    return _blocked('IO_FAILURE', cid, failed['seq'])
                def present(root):
                    try:
                        info = os.stat(cid, dir_fd=root, follow_symlinks=False)
                        if not stat.S_ISDIR(info.st_mode):
                            raise BootstrapRejected('IDENTITY_CHANGED')
                        return True
                    except FileNotFoundError:
                        return False
                source = present(ctx.roots['candidate_root'])
                target = present(ctx.roots['publish_root'])
                state = record['state']
                uncertain = state == 'PUBLISH_INTENT' and target
                if (source == target or state == 'PUBLISHED_UNACTIVATED' and source
                        or state in ('VERIFIED', 'APPROVED') and target):
                    raise BootstrapRejected('STATE_CONFLICT')
                selected = approval_id or record['approval_id']
                if selected is None or record['approval_id'] not in (None, selected):
                    raise BootstrapRejected('APPROVAL_MISMATCH')
                # A previous approver may have failed after rename, before fsync.
                os.fsync(ctx.roots['approval_root'])
                approval = load_approval(ctx, selected)
                if any(approval[k] != v for k,v in dict(candidate_id=cid,target_name=cid,
                       manifest_sha=record['manifest_sha'],policy_sha=ctx.deployment_sha).items()):
                    raise BootstrapRejected('APPROVAL_MISMATCH')
                def verify(candidate):
                    ctx.verify_candidate_quota(cid, candidate)
                    raw = read_protected_record(candidate, 'manifest.json', {ctx.config['creator_uid']})
                    manifest = validate_record(parse_record(raw), 'manifest')
                    if (manifest['format_version'] != 3 or digest(raw) != record['manifest_sha']
                            or manifest['candidate_id'] != cid
                            or manifest['candidate_directory_identity'] != directory_identity(candidate)
                            or manifest['resource_policy_sha'] != ctx.config['resource_policy_sha']
                            or manifest['audit_header_sha'] != initial['header_sha']):
                        raise BootstrapRejected('IDENTITY_CHANGED')
                    registry.platform_evidence(log, manifest['platform_evidence_sha'], ctx.deployment_sha)
                    facts = artifact.verify_frozen_database(candidate,
                        max_bytes=ctx.resource['max_candidate_bytes'], deadline=deadline)
                    if any(manifest[k] != v for k,v in facts.items()):
                        raise BootstrapRejected('IDENTITY_CHANGED')
                    budget(ctx,deadline,candidate,readback=target or state=='PUBLISHED_UNACTIVATED')
                with _directory(ctx.roots['candidate_root' if source else 'publish_root'],cid) as candidate:
                    verify(candidate)
                    if state == 'PUBLISHED_UNACTIVATED':
                        require_slots(log.inspect(), operation='readback')
                    else:
                        require_slots(log.inspect(), operation='advance')
                        if state == 'VERIFIED':
                            record = registry.append_state(log,'APPROVED',approval_id=selected)
                            state, seq = record['state'],record['seq']
                        if state == 'APPROVED':
                            ctx.revalidate()
                            budget(ctx,deadline,candidate)
                            record = registry.append_state(log,'PUBLISH_INTENT')
                            state, seq = record['state'],record['seq']
                        ctx.revalidate()
                        budget(ctx,deadline,candidate,readback=not source)
                        if source:
                            uncertain = True
                            try:
                                from api.display_bootstrap_growth_gate import before_growth
                                before_growth(ctx)
                                storage.rename_no_replace(ctx.roots['candidate_root'],cid,ctx.roots['publish_root'],cid)
                            except OSError as exc:
                                import errno
                                if exc.errno in (errno.EXDEV, errno.ENOSYS):
                                    uncertain = False
                                    raise BootstrapRejected('UNSUPPORTED_PLATFORM') from exc
                                raise
                            source, target = False, True
                        os.fsync(ctx.roots['candidate_root'])
                        os.fsync(ctx.roots['publish_root'])
                        with _directory(ctx.roots['publish_root'],cid) as moved:
                            verify(moved)
                        record = registry.append_state(log,'PUBLISHED_UNACTIVATED')
                        seq = record['seq']
                    return dict(format_version=1,status='PUBLISHED_UNACTIVATED',code='OK',candidate_id=cid,
                        target_name=cid,registry_seq=seq,manifest_sha=record['manifest_sha'],
                        completion_record_sha=log.inspect()['registry_sha'])
    except (OSError,ValueError,sqlite3.Error) as exc:
        code = _publication_error_code(exc)
        result = _blocked(code,cid,seq)
        if uncertain:
            result.update(status='UNCERTAIN',code='IO_FAILURE')
        elif code in ('IDENTITY_CHANGED','STATE_CONFLICT'):
            result.update(status='QUARANTINED',quarantine_persisted=False)
        return result


def budget(ctx, deadline, candidate_fd=None, *, admission=False, readback=False):
    from api.display_bootstrap_audit_log import _deadline
    from pathlib import Path
    _deadline(deadline)
    policy = ctx.resource
    rss = int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
    if rss > policy['max_rss_bytes']:
        raise BootstrapRejected('RESOURCE_LIMIT')
    if not readback:
        candidate = os.fstatvfs(ctx.roots['candidate_root'])
        needed = policy['min_free_bytes'] + (policy['max_candidate_bytes'] if admission else 0)
        if candidate.f_bavail * candidate.f_frsize < needed:
            raise BootstrapRejected('RESOURCE_LIMIT')
    if admission:
        audit = os.fstatvfs(ctx.roots['registry_root'])
        if (audit.f_bavail*audit.f_frsize < policy['min_free_bytes']+policy['audit_reserve_bytes']
                or audit.f_favail < 2 or candidate.f_favail < 2):
            raise BootstrapRejected('RESOURCE_LIMIT')
        names = set()
        for key in ('candidate_root','publish_root','registry_root'):
            with os.scandir(ctx.roots[key]) as entries:
                for entry in entries:
                    _deadline(deadline)
                    names.add(entry.name)
                    if len(names) >= policy['max_retained_candidates']:
                        raise BootstrapRejected('RESOURCE_LIMIT')
    if candidate_fd is not None:
        logical = allocated = 0
        with os.scandir(candidate_fd) as entries:
            for entry in entries:
                _deadline(deadline)
                info = entry.stat(follow_symlinks=False)
                logical += info.st_size
                allocated += info.st_blocks*512
                if max(logical,allocated) > policy['max_candidate_bytes']:
                    raise BootstrapRejected('RESOURCE_LIMIT')
    _deadline(deadline)
