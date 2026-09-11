"""Preparation failures retain candidate identity and never enter building."""
import time
import pytest
from api import display_bootstrap_admission as admission
from api import display_bootstrap_admission_ownership as ownership
from api import display_bootstrap_preparation_steps as steps


@pytest.mark.parametrize('stage', ['prepare', 'install', 'verify', 'consume'])
def test_failure_keeps_candidate_id_and_closes_once(monkeypatch, stage):
    events = []
    p = ownership._new_preparation(time.monotonic()+30)
    p.owner.callback(events.append, 'closed')
    monkeypatch.setattr(admission, '_prepare', lambda *a: p)
    def prepare(value):
        value.candidate_id, value.mode = 'b'*32, 'CREATE'
        if stage == 'prepare':
            raise ValueError('RESOURCE_LIMIT')
    monkeypatch.setattr(steps, 'prepare_create', prepare)
    def install(value):
        if stage == 'install':
            raise ValueError('RESOURCE_LIMIT')
    monkeypatch.setattr(admission, 'install', install)
    def verify(value, baseline=None):
        if stage == 'verify' or (stage == 'consume' and baseline is not None):
            raise ValueError('RESOURCE_LIMIT')
        return ()
    monkeypatch.setattr(admission, 'verify_prepared', verify)
    result = admission.execute_operation('a'*32, 'create')
    assert result['status'] == 'BLOCKED'
    assert result['code'] == 'RESOURCE_LIMIT'
    assert result['candidate_id'] == 'b'*32
    assert events == ['closed']
    assert id(p) not in ownership._PREPARATIONS
    assert not any(entry[1] is p for entry in ownership._RECEIPTS.values())
