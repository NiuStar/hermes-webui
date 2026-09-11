"""Real SQLite/filesystem faults; no privileged isolation or extra devices."""
import hashlib
import sqlite3
from contextlib import contextmanager

import pytest

from api.application_operation_issue import ApplicationIssue
from tests.test_application_v2 import runtime, _id, _request


def prepared(service, config, operation):
    cid, aid = _id(9100), _id(9101)
    if operation == 'recover':
        from unittest.mock import patch
        original = _request(_id(9105), 'create', {'candidate_id': cid})
        with patch.object(service.files, 'create', side_effect=OSError('interrupted create')):
            with pytest.raises(ApplicationIssue):
                service.submit(original, config.principal('creator'))
        actor = config.principal('publisher')
        observed = service.inspect_recovery(original['task_id'], cid, actor)
        return _request(_id(9106), 'recover', {
            'candidate_id': cid, 'interrupted_task_id': original['task_id'],
            'decision': 'close_failed', 'expected_evidence_sha': observed['evidence_sha'], 'approval_id': None,
        }), actor
    if operation != 'create':
        created = service.submit(_request(_id(9102), 'create', {'candidate_id': cid}), config.principal('creator'))
        params = {'candidate_id': cid, 'approval_id': aid, 'manifest_sha': created['result']['manifest_sha'],
                  'policy_sha': config.artifact_policy_sha, 'reference': 'fault-matrix-only'}
        if operation == 'approve':
            return _request(_id(9103), operation, params), config.principal('approver')
        service.submit(_request(_id(9103), 'approve', params), config.principal('approver'))
    return _request(_id(9104), operation, {'candidate_id': cid, **({'approval_id': aid} if operation == 'publish' else {})}), config.principal('publisher' if operation == 'publish' else 'creator')


@pytest.mark.parametrize('operation,commit_index', [(op, n) for op, count in [('create', 4), ('approve', 4), ('publish', 5), ('recover', 3)] for n in range(1, count + 1)])
@pytest.mark.parametrize('committed', [False, True])
def test_commit_response_unknown_real_write_sets(runtime, monkeypatch, operation, commit_index, committed):
    service, config = runtime
    raw, actor = prepared(service, config, operation)
    original = service.ledger.session
    seen = []
    fired = []

    class Connection:
        def __init__(self, db): self.db = db
        def __getattr__(self, name): return getattr(self.db, name)
        def execute(self, sql, *args):
            if sql == 'COMMIT':
                seen.append(sql)
                if len(seen) == commit_index:
                    fired.append(True)
                    if committed:
                        self.db.execute(sql, *args)
                    raise sqlite3.OperationalError('injected lost commit response')
            return self.db.execute(sql, *args)

    @contextmanager
    def session(readonly=False):
        with original(readonly=readonly) as db:
            yield db if readonly else Connection(db)

    with monkeypatch.context() as patch:
        patch.setattr(service.ledger, 'session', session)
        if committed:
            result = service.submit(raw, actor)
            assert result['task_state'] == 'SUCCEEDED'
        else:
            with pytest.raises(ApplicationIssue) as error:
                service.submit(raw, actor)
            assert error.value.code == 'OUTCOME_UNKNOWN'
    assert fired == [True]
    with original(readonly=True) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    if committed:
        assert service.submit(raw, actor) == result
    else:
        stored = service.ledger.lookup(raw['task_id'], actor)
        assert stored is None or stored['state'] != 'SUCCEEDED'


@pytest.mark.parametrize("operation", ["create", "approve", "publish"])
@pytest.mark.parametrize("tamper", [False, True])
def test_half_written_original_audit_is_bound_by_recovery(runtime, monkeypatch, tamper, operation):
    service, config = runtime
    raw, actor = prepared(service, config, operation)
    recovery_actor = config.principal('publisher')
    audit_path = service.files.audits / (raw['task_id'] + '.json')
    partial = b'{"mode":"application_runtime_v2",'

    def half_write(*args, **kwargs):
        audit_path.write_bytes(partial)
        raise OSError('injected audit short write')

    with monkeypatch.context() as patch:
        patch.setattr(service.files, 'audit', half_write)
        with pytest.raises(OSError):
            service.submit(raw, actor)
    observed = service.inspect_recovery(raw['task_id'], raw['parameters']['candidate_id'], recovery_actor)
    recovery = _request(_id(9199), 'recover', {
        'candidate_id': raw['parameters']['candidate_id'], 'interrupted_task_id': raw['task_id'],
        'decision': 'finalize_existing', 'expected_evidence_sha': observed['evidence_sha'], 'approval_id': raw['parameters'].get('approval_id'),
    })
    if tamper:
        audit_path.write_bytes(partial + b' ')
        with pytest.raises(ApplicationIssue) as error:
            service.submit(recovery, recovery_actor)
        assert error.value.code == 'CONFLICT'
        assert service.ledger.lookup(recovery['task_id'], recovery_actor) is None
        return
    result = service.submit(recovery, recovery_actor)
    assert result['task_state'] == 'SUCCEEDED' 
    assert audit_path.read_bytes() == partial
    with service.ledger.session(readonly=True) as db:
        row = db.execute("SELECT sha,record_state FROM audit_refs WHERE owner_id=? AND kind='REPAIR'", (recovery['task_id'],)).fetchone()
        assert row is not None, 'recovery must bind the preserved half-written original audit'
        assert row['sha'] == hashlib.sha256(partial).hexdigest()
        assert row['record_state'] == 'PARTIAL'

@pytest.mark.parametrize("seam", ["intent_before", "intent_after", "rename_before", "rename_after"])
def test_publish_interruption_matrix(runtime, monkeypatch, seam):
    from api import application_artifact_files as files_api
    service, config = runtime
    raw, actor = prepared(service, config, 'publish')
    cid = raw['parameters']['candidate_id']
    original_intent = service._intent
    original_rename = files_api._rename_no_replace
    fired = []

    def intent(*args, **kwargs):
        fired.append(seam)
        if seam == 'intent_after':
            original_intent(*args, **kwargs)
        raise RuntimeError('injected intention interruption')

    def rename(*args, **kwargs):
        fired.append(seam)
        if seam == 'rename_after':
            original_rename(*args, **kwargs)
        raise OSError('injected rename interruption')

    with monkeypatch.context() as patch:
        if seam.startswith('intent'):
            patch.setattr(service, '_intent', intent)
        else:
            patch.setattr(files_api, '_rename_no_replace', rename)
        with pytest.raises((RuntimeError, ApplicationIssue)):
            service.submit(raw, actor)
    assert fired == [seam]
    observed = service.inspect_recovery(raw['task_id'], cid, actor)
    assert observed['decision'] == ('finalize_existing' if seam == 'rename_after' else 'resume_publish')
    recovery = _request(_id(9200), 'recover', {
        'candidate_id': cid, 'interrupted_task_id': raw['task_id'],
        'decision': observed['decision'], 'expected_evidence_sha': observed['evidence_sha'],
        'approval_id': raw['parameters']['approval_id'],
    })
    result = service.submit(recovery, actor)
    assert result['task_state'] == 'SUCCEEDED'
    assert result['result']['business_status'] == 'PUBLISHED_UNACTIVATED'
    assert result['result']['activated'] is False
    assert not (service.files.candidates / cid).exists()
    assert (service.files.published / cid / 'display.sqlite').is_file()
    assert service.submit(recovery, actor) == result
