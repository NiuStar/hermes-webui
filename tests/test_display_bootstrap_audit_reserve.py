"""Audit primitive tests; run only in the authorized isolated .10 workspace.

These do not establish independent-volume admission or metadata guarantees.
"""
import os
import pytest
from api import display_bootstrap_audit_reserve as reserve
from api.display_bootstrap_policy import BootstrapRejected


@pytest.fixture
def directories(tmp_path):
    source, target = tmp_path/'reserve', tmp_path/'records'
    source.mkdir(mode=0o700)
    target.mkdir(mode=0o700)
    fds = [os.open(p, os.O_RDONLY | os.O_DIRECTORY) for p in (source, target)]
    try:
        yield source, target, *fds
    finally:
        for fd in fds:
            os.close(fd)


def test_preallocate_consume_and_refuse_exhaustion(directories):
    source, target, reserve_fd, target_fd = directories
    result = reserve.provision(reserve_fd, reserve_bytes=reserve.SLOT_BYTES)
    assert result['available_bytes'] == reserve.SLOT_BYTES
    raw = b'{"version":1}'
    reserve.write_reserved(reserve_fd, target_fd, 'one.json', raw)
    assert (target/'one.json').read_bytes() == raw
    assert reserve.inspect(reserve_fd)['available_bytes'] == 0
    with pytest.raises(BootstrapRejected, match='AUDIT_UNAVAILABLE'):
        reserve.write_reserved(reserve_fd, target_fd, 'two.json', raw)
    assert not (target/'two.json').exists()


def test_conflict_retains_claim_and_original(directories):
    source, target, reserve_fd, target_fd = directories
    reserve.provision(reserve_fd, reserve_bytes=reserve.SLOT_BYTES)
    (target/'one.json').write_bytes(b'preserved')
    with pytest.raises(FileExistsError):
        reserve.write_reserved(reserve_fd, target_fd, 'one.json', b'{"version":1}')
    assert (target/'one.json').read_bytes() == b'preserved'
    result = reserve.inspect(reserve_fd)
    assert result['available_slots'] == []
    assert result['retained_claims'] == ['claimed-00000000']
    assert (source/'claimed-00000000').read_bytes() == b'{"version":1}'


def test_invalid_budget_does_not_provision(directories):
    source, _, reserve_fd, _ = directories
    with pytest.raises(BootstrapRejected, match='INVALID_INPUT'):
        reserve.provision(reserve_fd, reserve_bytes=1)
    assert list(source.iterdir()) == []


@pytest.mark.parametrize('budget', [True, 0, -1, 65535, 4097 * 65536])
def test_budget_rejected_before_storage_access(monkeypatch, budget):
    def forbidden(*args):
        pytest.fail('invalid budget touched storage')
    monkeypatch.setattr(reserve, '_directory', forbidden)
    with pytest.raises(BootstrapRejected, match='INVALID_INPUT'):
        reserve.provision(-1, reserve_bytes=budget)


def test_duplicate_claim_index_is_not_available(directories):
    source, _, fd, _ = directories
    reserve.provision(fd, reserve_bytes=reserve.SLOT_BYTES)
    claim = source / 'claimed-00000000'
    claim.touch(mode=0o600)
    with pytest.raises(BootstrapRejected, match='STATE_CONFLICT'):
        reserve.inspect(fd)
    assert claim.exists()
    assert (source / 'slot-00000000').exists()


def test_out_of_range_index_rejected(directories):
    source, _, fd, _ = directories
    (source / 'claimed-00004096').touch(mode=0o600)
    with pytest.raises(BootstrapRejected, match='STATE_CONFLICT'):
        reserve.inspect(fd)


def test_scan_overflow_is_bounded(directories, monkeypatch):
    source, _, fd, _ = directories
    (source / 'claimed-00000000').touch(mode=0o600)
    (source / 'claimed-00000001').touch(mode=0o600)
    monkeypatch.setattr(reserve, 'MAX_SLOTS', 1)
    with pytest.raises(BootstrapRejected, match='AUDIT_UNAVAILABLE'):
        reserve.inspect(fd)
    assert os.fstat(fd).st_ino == source.stat().st_ino

