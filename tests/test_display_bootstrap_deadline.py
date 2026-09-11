"""Cooperative context budget tests; no platform admission claim."""
import os
import time
import pytest
from api.display_bootstrap_policy import BootstrapContext, BootstrapRejected


def context():
    ctx = object.__new__(BootstrapContext)
    ctx.pid, ctx.closed = os.getpid(), False
    ctx.started_monotonic = 100.0
    ctx.resource = {'max_elapsed_seconds': 10}
    return ctx


def test_repeated_calls_do_not_extend_deadline(monkeypatch):
    ctx = context()
    monkeypatch.setattr(time, 'monotonic', lambda: 101.0)
    assert ctx.operation_deadline() == 110.0
    monkeypatch.setattr(time, 'monotonic', lambda: 109.0)
    assert ctx.operation_deadline() == 110.0
    monkeypatch.setattr(time, 'monotonic', lambda: 110.0)
    with pytest.raises(BootstrapRejected, match='RESOURCE_LIMIT'):
        ctx.operation_deadline()


@pytest.mark.parametrize('start', [None, True, float('nan'), float('inf'), 102.0])
def test_untrusted_clock_origin_rejected(monkeypatch, start):
    ctx = context()
    ctx.started_monotonic = start
    monkeypatch.setattr(time, 'monotonic', lambda: 101.0)
    with pytest.raises(BootstrapRejected, match='ACCESS_BOUNDARY_UNPROVEN'):
        ctx.operation_deadline()


def test_closed_context_cannot_request_budget():
    ctx = context()
    ctx.closed = True
    with pytest.raises(BootstrapRejected, match='ACCESS_BOUNDARY_UNPROVEN'):
        ctx.operation_deadline()


def test_missing_origin_does_not_start_new_budget():
    ctx = context()
    del ctx.started_monotonic
    with pytest.raises(BootstrapRejected, match='ACCESS_BOUNDARY_UNPROVEN'):
        ctx.operation_deadline()
