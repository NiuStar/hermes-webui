"""Pure budget comparisons and fixed-unit throttling without real unit starts."""
import pytest
from api.display_bootstrap_growth_math import require_headroom
from api.display_bootstrap_start_limiter import StartLimiter


def test_headroom_strict_threshold():
    committed = dict(bytes=100, inodes=10)
    used = dict(bytes=50, inodes=5)
    reserve = dict(bytes=20, inodes=2)
    require_headroom(committed, used, dict(bytes=71, inodes=8), reserve)
    with pytest.raises(ValueError, match='RESOURCE_LIMIT'):
        require_headroom(committed, used, dict(bytes=70, inodes=8), reserve)


def test_no_negative_debt_hides_overgrowth():
    with pytest.raises(ValueError, match='RESOURCE_LIMIT'):
        require_headroom(dict(bytes=100, inodes=10), dict(bytes=101, inodes=5),
                         dict(bytes=1000, inodes=100), dict(bytes=1, inodes=1))


def test_clock_guard_and_sixth_start(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('api.display_bootstrap_start_limiter.time.monotonic', lambda: clock[0])
    limiter = StartLimiter(('hermes-bootstrap-worker.service',))
    with pytest.raises(ValueError, match='RESOURCE_LIMIT'):
        limiter.consume('hermes-bootstrap-worker.service')
    clock[0] = 160.0
    for _ in range(5):
        limiter.consume('hermes-bootstrap-worker.service')
    with pytest.raises(ValueError, match='RESOURCE_LIMIT'):
        limiter.consume('hermes-bootstrap-worker.service')
    clock[0] = 220.0
    limiter.consume('hermes-bootstrap-worker.service')
    clock[0] = 219.0
    with pytest.raises(ValueError, match='ACCESS_BOUNDARY_UNPROVEN'):
        limiter.consume('hermes-bootstrap-worker.service')
