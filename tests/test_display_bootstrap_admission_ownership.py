"""Receipt ownership rejection and cleanup contracts; no runtime admission."""
import os
import time
import pytest
from api import display_bootstrap_admission_ownership as ownership


def prep():
    return ownership._new_preparation(time.monotonic()+30)


def test_public_constructors_reject():
    for constructor in (ownership._Preparation, ownership._Receipt):
        with pytest.raises(ValueError):
            constructor()


def test_dictionary_is_not_receipt():
    with pytest.raises(ValueError):
        ownership._consume_receipt({}, lambda *a: None, lambda *a: None)


def test_failure_consumes_receipt_and_closes_once():
    value = prep()
    events = []
    value.owner.callback(events.append, 'closed')
    receipt = ownership._register_receipt(value, ())
    def fail(*args):
        raise ValueError('injected')
    with pytest.raises(ValueError, match='injected'):
        ownership._consume_receipt(receipt, fail, lambda *a: None)
    assert events == ['closed']
    with pytest.raises(ValueError):
        ownership._consume_receipt(receipt, lambda *a: None, lambda *a: None)
    value.close()
    assert events == ['closed']


def test_transfer_failure_closes_only_transferred_owner():
    value = prep()
    events = []
    value.owner.callback(events.append, 'closed')
    receipt = ownership._register_receipt(value, ())
    def fail(*args):
        raise ValueError('construction')
    with pytest.raises(ValueError, match='construction'):
        ownership._consume_receipt(receipt, lambda *a: None, fail)
    value.close()
    assert events == ['closed']


def test_success_transfers_and_old_preparation_cannot_close():
    value = prep()
    events = []
    value.owner.callback(events.append, 'closed')
    receipt = ownership._register_receipt(value, ())
    result = ownership._consume_receipt(receipt, lambda *a: None, lambda p,v,owner: owner)
    value.close()
    assert events == []
    result.close()
    assert events == ['closed']


def test_cross_pid_rejected():
    value = prep()
    value.pid = os.getpid()+1
    try:
        with pytest.raises(ValueError):
            ownership._register_receipt(value, ())
    finally:
        value.close()


def test_duplicate_receipt_rejected():
    value = prep()
    try:
        ownership._register_receipt(value, ())
        with pytest.raises(ValueError):
            ownership._register_receipt(value, ())
    finally:
        value.close()
