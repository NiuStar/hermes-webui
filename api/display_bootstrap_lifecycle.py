"""Offline candidate orchestration. Never activates or returns a connection.

Context admission owns the global lock and root FDs; each operation owns only
its child FDs. All failures retain files. This module has no runtime wiring.
"""
import re
import errno
import os
import secrets
import sqlite3
import stat
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from api import display_bootstrap_artifact as artifact
from api import display_bootstrap_publish as storage
from api.display_bootstrap_manifest import canonical_bytes, parse_record, validate_record, digest
from api.display_bootstrap_policy import directory_identity, read_protected_record

from api.display_bootstrap_policy import BootstrapContext, BootstrapRejected


def _creator_commit(ctx):
    ctx.check_owner()
    ctx.release_handle.revalidate()
    return ctx.release_handle.commit

@contextmanager
def _directory(parent, name, *, create=False):
    if create:
        os.mkdir(name, 0o700, dir_fd=parent)
        os.fsync(parent)
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        value = os.fstat(fd)
        if (value.st_uid != os.geteuid() or value.st_mode & 0o077
                or any(k.startswith('system.posix_acl_') for k in os.listxattr(fd))):
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        yield fd
    finally:
        os.close(fd)


def _write_audit(registry_fd, target_fd, name, raw):
    """Every lifecycle audit write consumes its candidate's durable reserve."""
    with _directory(registry_fd, '.audit-reserve') as reserve:
        storage.write_record(target_fd, name, raw, reserve_fd=reserve)


def _require_audit_slots(registry_fd, count):
    from api.display_bootstrap_audit_reserve import inspect
    with _directory(registry_fd, '.audit-reserve') as reserve:
        if len(inspect(reserve)['available_slots']) < count:
            raise BootstrapRejected('AUDIT_UNAVAILABLE')


def _prepare_audit(ctx, registry_fd):
    from api.display_bootstrap_audit_reserve import provision, validate_budget
    budget = ctx.resource['audit_reserve_bytes']
    # Six normal state records, platform evidence and two failure records.
    validate_budget(budget, minimum_slots=9)
    if os.fstat(registry_fd).st_dev == os.fstat(ctx.roots['candidate_root']).st_dev:
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
    # Create evidence metadata before any candidate data can consume space.
    with _directory(registry_fd, 'evidence', create=True):
        pass
    with _directory(registry_fd, '.audit-reserve', create=True) as reserve:
        provision(reserve, reserve_bytes=budget)
    os.fsync(registry_fd)


def _append(directory_fd, cid, state, *, manifest_sha=None, approval_id=None, error_code=None):
    chain = storage.read_registry(directory_fd, cid)
    prior = chain[-1] if chain else None
    record = dict(format_version=1, candidate_id=cid, target_name=cid,
                  seq=len(chain) + 1, previous_sha=digest(canonical_bytes(prior)) if prior else None,
                  state=state, manifest_sha=manifest_sha if not prior else
                  (prior['manifest_sha'] or manifest_sha),
                  approval_id=(prior['approval_id'] or approval_id) if prior else approval_id, error_code=error_code)
    validate_record(record, 'registry')
    try:
        _write_audit(directory_fd, directory_fd, f"{record['seq']:020d}.json", canonical_bytes(record))
    except (OSError, ValueError) as exc:
        raise BootstrapRejected('AUDIT_UNAVAILABLE') from exc
    actual = storage.read_registry(directory_fd, cid)
    if actual != chain + [record]:
        raise BootstrapRejected('STATE_CONFLICT')
    return record


def _budget(ctx, deadline, candidate_fd=None, *, reserve=False):
    p = ctx.resource
    if time.monotonic() >= deadline:
        raise BootstrapRejected('RESOURCE_LIMIT')
    rss = int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
    if rss > p['max_rss_bytes']:
        raise BootstrapRejected('RESOURCE_LIMIT')
    for key in ('candidate_root', 'registry_root'):
        space = os.fstatvfs(ctx.roots[key])
        required = p['min_free_bytes'] + p['audit_reserve_bytes']
        if reserve:
            required += p['max_candidate_bytes']
        if space.f_bavail * space.f_frsize < required:
            raise BootstrapRejected('RESOURCE_LIMIT')
    if reserve:
        retained = set().union(*(set(os.listdir(ctx.roots[k])) for k in
                               ('candidate_root', 'publish_root', 'registry_root')))
        if len(retained) >= p['max_retained_candidates']:
            raise BootstrapRejected('RESOURCE_LIMIT')
    if candidate_fd is not None:
        sizes = [os.stat(n, dir_fd=candidate_fd, follow_symlinks=False)
                 for n in os.listdir(candidate_fd)]
        if max(sum(s.st_size for s in sizes), sum(s.st_blocks * 512 for s in sizes)) > p['max_candidate_bytes']:
            raise BootstrapRejected('RESOURCE_LIMIT')


def _id(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{32}', value) is None:
        raise BootstrapRejected('INVALID_INPUT')
    return value


def _context(ctx):
    if type(ctx) is not BootstrapContext:
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
    ctx.check_owner()
    ctx.revalidate()


def _blocked(code, candidate_id=None, seq=None):
    return dict(format_version=1, status='BLOCKED', code=code,
                candidate_id=candidate_id, target_name=candidate_id, registry_seq=seq)


def _creation_failure(fd, cid, exc):
    if isinstance(exc, BootstrapRejected):
        code = exc.code
    elif isinstance(exc, sqlite3.Error):
        code = ('RESOURCE_LIMIT' if getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_FULL
                else 'IO_FAILURE')
    elif isinstance(exc, OSError):
        code = ('RESOURCE_LIMIT' if exc.errno in (errno.EDQUOT, errno.ENOSPC)
                else 'TARGET_CONFLICT' if exc.errno == errno.EEXIST else 'IO_FAILURE')
    else:
        code = str(exc)
        if code not in ('UNKNOWN_SCHEMA', 'SIDECAR_REMAINS', 'IDENTITY_CHANGED',
                        'RESOURCE_LIMIT', 'TARGET_CONFLICT', 'STATE_CONFLICT',
                        'UNSUPPORTED_PLATFORM', 'INVALID_INPUT'):
            raise exc
    quarantine = code in ('IDENTITY_CHANGED', 'STATE_CONFLICT')
    seq, persisted = None, False
    try:
        chain = storage.read_registry(fd, cid)
        seq = chain[-1]['seq'] if chain else None
        if code != 'AUDIT_UNAVAILABLE' and chain:
            # Preserve only safe error metadata, never SQLite messages or paths.
            # Provisioned before confinement; never create metadata after a
            # candidate failure or try to escape the write boundary.
            with _directory(fd, 'evidence') as audit:
                raw = canonical_bytes(dict(format_version=1, candidate_id=cid,
                    exception_type=type(exc).__name__, errno=getattr(exc, 'errno', None),
                    error_code=code))
                _write_audit(fd, audit, digest(raw) + '.json', raw)
            seq = _append(fd, cid, 'QUARANTINED' if quarantine else 'FAILED',
                          error_code=code)['seq']
            persisted = True
    except (OSError, ValueError):
        if not quarantine:
            code = 'AUDIT_UNAVAILABLE'
    result = _blocked(code, cid, seq)
    if quarantine:
        result.update(status='QUARANTINED', quarantine_persisted=persisted)
    return result


def _persist_evidence(ctx, registry):
    try:
        evidence = validate_record(ctx.evidence, 'platform_evidence')
        if (evidence['approved_policy_sha'] != ctx.deployment_sha or
                digest(canonical_bytes(ctx.resource)) != ctx.config['resource_policy_sha']):
            raise BootstrapRejected('APPROVAL_MISMATCH')
        raw = canonical_bytes(evidence)
        sha = digest(raw)
        # Reserve room for the chain plus evidence, including allocation rounding.
        if len(raw) + 8 * 65536 > ctx.resource['audit_reserve_bytes']:
            raise BootstrapRejected('AUDIT_UNAVAILABLE')
        with _directory(registry, 'evidence') as fd:
            _write_audit(registry, fd, sha + '.json', raw)
            actual = read_protected_record(fd, sha + '.json', {ctx.config['creator_uid']})
            if digest(actual) != sha or validate_record(parse_record(actual), 'platform_evidence') != evidence:
                raise BootstrapRejected('AUDIT_UNAVAILABLE')
        return sha
    except (OSError, ValueError) as exc:
        raise BootstrapRejected('AUDIT_UNAVAILABLE') from exc


def _create(ctx):
    if getattr(ctx, 'creation_confined', False) or getattr(ctx, 'publication_confined', False):
        return _blocked('ACCESS_BOUNDARY_UNPROVEN')
    cid = None
    try:
        deadline = ctx.operation_deadline()
        ctx.revalidate()
        validate_record(ctx.resource, 'resource')
        from api.display_bootstrap_audit_reserve import validate_budget
        validate_budget(ctx.resource['audit_reserve_bytes'], minimum_slots=9)
        if ctx.config['expected_ddl_sha'] != artifact.PINNED_DDL_SHA:
            raise BootstrapRejected('UNKNOWN_SCHEMA')
        _budget(ctx, deadline, reserve=True)
        commit = _creator_commit(ctx)
        cid = secrets.token_hex(16)
        with _directory(ctx.roots['registry_root'], cid, create=True) as registry:
            try:
                _prepare_audit(ctx, registry)
                _append(registry, cid, 'RESERVED')
                ctx.revalidate()
                with _directory(ctx.roots['candidate_root'], cid, create=True) as candidate:
                    identity = directory_identity(candidate)
                    ctx.prepare_candidate_quota(cid, candidate)
                    ctx.verify_candidate_quota(cid, candidate)
                    ctx.restrict_creation_writes(candidate, registry)
                    _append(registry, cid, 'BUILDING')
                    _budget(ctx, deadline, candidate)
                    facts = artifact.build_private_database(candidate,
                        max_bytes=ctx.resource['max_candidate_bytes'], deadline=deadline)
                    ctx.verify_candidate_quota(cid, candidate)
                    ctx.revalidate()
                    _budget(ctx, deadline, candidate)
                    evidence_sha = _persist_evidence(ctx, registry)
                    manifest = dict(facts, format_version=1, candidate_id=cid,
                        ddl_sha=artifact.PINNED_DDL_SHA, sqlite_version=sqlite3.sqlite_version,
                        platform_evidence_sha=evidence_sha,
                        resource_policy_sha=ctx.config['resource_policy_sha'],
                        candidate_directory_identity=identity,
                        connection_policy=dict(foreign_keys=1, synchronous=2), creator_commit=commit)
                    validate_record(manifest, 'manifest')
                    raw = canonical_bytes(manifest)
                    storage.write_record(candidate, 'manifest.json', raw)
                    _budget(ctx, deadline, candidate)
                    ctx.revalidate()
                    named = os.stat(cid, dir_fd=ctx.roots['candidate_root'], follow_symlinks=False)
                    if (directory_identity(candidate) != identity or
                            {k: getattr(named, 'st_' + k) for k in identity} != identity):
                        raise BootstrapRejected('IDENTITY_CHANGED')
                    ctx.verify_candidate_quota(cid, candidate)
                    _budget(ctx, deadline, candidate)
                    seq = _append(registry, cid, 'VERIFIED', manifest_sha=digest(raw))['seq']
                    return dict(format_version=1, status='VERIFIED', code='OK', candidate_id=cid,
                                target_name=cid, registry_seq=seq, manifest_sha=digest(raw))
            except (OSError, ValueError, sqlite3.Error) as exc:
                return _creation_failure(registry, cid, exc)
            except (KeyboardInterrupt, SystemExit) as exc:
                _creation_failure(registry, cid, BootstrapRejected('IO_FAILURE'))
                raise
    except BootstrapRejected as exc:
        return _blocked(exc.code, cid)
    except OSError:
        return _blocked('AUDIT_UNAVAILABLE' if cid else 'IO_FAILURE', cid)


def create_candidate(ctx):
    try:
        _context(ctx)
    except BootstrapRejected as exc:
        return _blocked(exc.code)
    if ctx.config.get('format_version') == 3:
        from api.display_bootstrap_fixed_lifecycle import create
        return create(ctx)
    return _blocked('ACCESS_BOUNDARY_UNPROVEN')  # Legacy media is read-only.


def load_approval(ctx, approval_id):
    _id(approval_id)
    _context(ctx)
    try:
        raw = read_protected_record(ctx.roots['approval_root'], approval_id + '.json',
                                    {ctx.config['approver_uid']})
        record = validate_record(parse_record(raw), 'approval')
        if ctx.config.get('format_version') == 3:
            from api.display_bootstrap_runner_binding import resolve_runner_binding
            binding = resolve_runner_binding(ctx.config, ctx.resource, 'approver')
            if record['format_version'] != 2 or record.get('actor') != binding.actor():
                raise ValueError('APPROVAL_MISMATCH')
        if record['approval_id'] != approval_id:
            raise ValueError('APPROVAL_MISMATCH')
        return record
    except (OSError, ValueError) as exc:
        raise BootstrapRejected('APPROVAL_MISMATCH') from exc


def _verify_candidate(ctx, registry, candidate, cid, record, deadline):
    # Applies to source, moved target and restart recovery alike.
    ctx.verify_candidate_quota(cid, candidate)
    raw = read_protected_record(candidate, 'manifest.json', {ctx.config['creator_uid']})
    if digest(raw) != record['manifest_sha']:
        raise BootstrapRejected('IDENTITY_CHANGED')
    manifest = validate_record(parse_record(raw), 'manifest')
    if manifest['candidate_id'] != cid or directory_identity(candidate) != manifest['candidate_directory_identity']:
        raise BootstrapRejected('IDENTITY_CHANGED')
    with _directory(registry, 'evidence') as evidence_fd:
        evidence_raw = read_protected_record(evidence_fd, manifest['platform_evidence_sha'] + '.json',
                                             {ctx.config['creator_uid']})
    evidence = validate_record(parse_record(evidence_raw), 'platform_evidence')
    if (digest(evidence_raw) != manifest['platform_evidence_sha']
            or evidence['approved_policy_sha'] != ctx.deployment_sha
            or manifest['resource_policy_sha'] != ctx.config['resource_policy_sha']
            or digest(canonical_bytes(ctx.resource)) != manifest['resource_policy_sha']):
        raise BootstrapRejected('APPROVAL_MISMATCH')
    facts = artifact.verify_frozen_database(candidate, max_bytes=ctx.resource['max_candidate_bytes'], deadline=deadline)
    if any(manifest[key] != value for key, value in facts.items()):
        raise BootstrapRejected('IDENTITY_CHANGED')
    _budget(ctx, deadline, candidate)
    ctx.verify_candidate_quota(cid, candidate)
    return manifest


def _publication_error_code(exc):
    if isinstance(exc, BootstrapRejected):
        return exc.code
    if isinstance(exc, OSError) and exc.errno in (errno.EDQUOT, errno.ENOSPC):
        return 'RESOURCE_LIMIT'
    if isinstance(exc, sqlite3.Error):
        return ('RESOURCE_LIMIT' if getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_FULL
                else 'IO_FAILURE')
    if type(exc) is ValueError and str(exc) in (
            'INVALID_INPUT', 'AUDIT_UNAVAILABLE', 'ACCESS_BOUNDARY_UNPROVEN',
            'APPROVAL_MISMATCH', 'POLICY_MISSING',
            'IDENTITY_CHANGED', 'STATE_CONFLICT', 'RESOURCE_LIMIT',
            'UNKNOWN_SCHEMA', 'SIDECAR_REMAINS', 'UNSUPPORTED_PLATFORM'):
        return str(exc)
    return 'IO_FAILURE'


def _publish(ctx, cid, approval_id):
    if getattr(ctx, 'creation_confined', False):
        return _blocked('ACCESS_BOUNDARY_UNPROVEN', cid)
    seq = None
    uncertain = False
    try:
        with _directory(ctx.roots['registry_root'], cid) as registry:
            ctx.restrict_publication_writes(registry)
            chain = storage.read_registry(registry, cid)
            if not chain:
                return _blocked('STATE_CONFLICT', cid)
            record = chain[-1]
            seq = record['seq']
            state = record['state']
            if state == 'FAILED':
                return _blocked('STATE_CONFLICT', cid, seq)
            if state == 'QUARANTINED':
                return dict(_blocked('STATE_CONFLICT', cid, seq), status='QUARANTINED', quarantine_persisted=True)
            if state in ('RESERVED', 'BUILDING'):
                return _creation_failure(registry, cid, BootstrapRejected('IO_FAILURE'))
            def present(root):
                try:
                    value = os.stat(cid, dir_fd=root, follow_symlinks=False)
                    if not stat.S_ISDIR(value.st_mode):
                        raise BootstrapRejected('IDENTITY_CHANGED')
                    return True
                except FileNotFoundError:
                    return False
            source, target = present(ctx.roots['candidate_root']), present(ctx.roots['publish_root'])
            if source == target or (state == 'PUBLISHED_UNACTIVATED' and source) or (state in ('VERIFIED','APPROVED') and target):
                return _creation_failure(registry, cid, BootstrapRejected('STATE_CONFLICT'))
            # A previous process may have moved the directory without syncing
            # completion. Preserve that uncertainty during verification too,
            # not only once this process reaches the rename/fsync section.
            uncertain = state == 'PUBLISH_INTENT' and target and not source
            selected = approval_id or record['approval_id']
            if selected is None or (record['approval_id'] is not None and selected != record['approval_id']):
                return _blocked('APPROVAL_MISMATCH', cid, seq)
            approval = load_approval(ctx, selected)
            if any(approval[k] != v for k,v in dict(candidate_id=cid, target_name=cid,
                    manifest_sha=record['manifest_sha'], policy_sha=ctx.deployment_sha).items()):
                return _blocked('APPROVAL_MISMATCH', cid, seq)
            deadline = ctx.operation_deadline()
            parent = ctx.roots['candidate_root' if source else 'publish_root']
            with _directory(parent, cid) as candidate:
                _verify_candidate(ctx, registry, candidate, cid, record, deadline)
                # Reserve remaining normal records plus failure evidence/state.
                remaining = {'VERIFIED': 5, 'APPROVED': 4, 'PUBLISH_INTENT': 3,
                             'PUBLISHED_UNACTIVATED': 0}
                if state not in remaining:
                    raise BootstrapRejected('STATE_CONFLICT')
                _require_audit_slots(registry, remaining[state])
                _budget(ctx, deadline, candidate)
                if state == 'VERIFIED':
                    record = _append(registry, cid, 'APPROVED', approval_id=selected)
                    state, seq = record['state'], record['seq']
                if state == 'APPROVED':
                    _budget(ctx, deadline, candidate)
                    record = _append(registry, cid, 'PUBLISH_INTENT')
                    state, seq = record['state'], record['seq']
                ctx.revalidate()
                _budget(ctx, deadline, candidate)
                if state != 'PUBLISHED_UNACTIVATED':
                    uncertain = not source
                    if source:
                        uncertain = True
                        try:
                            storage.rename_no_replace(ctx.roots['candidate_root'], cid, ctx.roots['publish_root'], cid)
                        except OSError as exc:
                            if exc.errno in (errno.EXDEV, errno.ENOSYS):
                                uncertain = False
                                raise BootstrapRejected('UNSUPPORTED_PLATFORM') from exc
                            raise
                    os.fsync(ctx.roots['candidate_root'])
                    os.fsync(ctx.roots['publish_root'])
                    with _directory(ctx.roots['publish_root'], cid) as published:
                        _verify_candidate(ctx, registry, published, cid, record, deadline)
                    record = _append(registry, cid, 'PUBLISHED_UNACTIVATED')
                    seq = record['seq']
                return dict(format_version=1, status='PUBLISHED_UNACTIVATED', code='OK',
                            candidate_id=cid, target_name=cid, registry_seq=seq,
                            manifest_sha=record['manifest_sha'], completion_record_sha=digest(canonical_bytes(record)))
    except (OSError, ValueError, sqlite3.Error) as exc:
        code = _publication_error_code(exc)
        if code in ('IDENTITY_CHANGED', 'STATE_CONFLICT'):
            try:
                with _directory(ctx.roots['registry_root'], cid) as registry:
                    return _creation_failure(registry, cid, BootstrapRejected(code))
            except (OSError, ValueError):
                return dict(_blocked(code, cid, seq), status='QUARANTINED', quarantine_persisted=False)
        if uncertain:
            return dict(_blocked('IO_FAILURE', cid, seq), status='UNCERTAIN')
        return _blocked(code, cid, seq)


def publish_candidate(ctx, candidate_id, approval_id):
    try:
        _id(candidate_id)
        _id(approval_id)
    except BootstrapRejected as exc:
        return _blocked(exc.code)
    try:
        _context(ctx)
    except BootstrapRejected as exc:
        return _blocked(exc.code, candidate_id)
    if ctx.config.get('format_version') == 3:
        from api.display_bootstrap_fixed_lifecycle import publish
        return publish(ctx, candidate_id, approval_id)
    return _blocked('ACCESS_BOUNDARY_UNPROVEN', candidate_id)


def recover_candidate(ctx, candidate_id):
    try:
        _id(candidate_id)
    except BootstrapRejected as exc:
        return _blocked(exc.code)
    try:
        _context(ctx)
    except BootstrapRejected as exc:
        return _blocked(exc.code, candidate_id)
    if ctx.config.get('format_version') == 3:
        from api.display_bootstrap_fixed_lifecycle import publish
        return publish(ctx, candidate_id, None)
    return _blocked('ACCESS_BOUNDARY_UNPROVEN', candidate_id)
