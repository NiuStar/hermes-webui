"""Retained ledger regression cases; kernel and peer admission are separate."""
import os
import pytest
from api import display_bootstrap_quota_ledger as ledger
from api.display_bootstrap_manifest import canonical_bytes
from api.display_bootstrap_policy import BootstrapRejected, directory_identity


@pytest.fixture
def held(tmp_path):
    if os.geteuid() != 0:
        pytest.fail('ledger tests require the authorized isolated root test runner')
    tmp_path.chmod(0o700)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield tmp_path, fd
    finally:
        os.close(fd)


def binding(fd, cid):
    identity = directory_identity(fd)
    return dict(version=1, candidate_id=cid, policy_sha='a'*64,
                directory_identity=identity, device=identity['dev'],
                hard_bytes=8388608, hard_inodes=64, project_id=1001)


def store(path, cid, value):
    record = path / (cid+'.json')
    record.write_bytes(canonical_bytes(value))
    record.chmod(0o600)


def test_canonical_filename_is_accepted(held):
    path, fd = held
    cid = '1'*32
    value = binding(fd, cid)
    store(path, cid, value)
    assert ledger.validate_index(fd, 1001, 1100) == {cid: value}


def test_duplicate_identifier_blocks_existing_retry(held):
    path, fd = held
    first, second = '1'*32, '2'*32
    value = binding(fd, first)
    store(path, first, value)
    store(path, second, dict(value, candidate_id=second))
    with pytest.raises(BootstrapRejected, match='STATE_CONFLICT'):
        ledger.reserve(fd, first, first_id=1001, last_id=1100,
            policy_sha=value['policy_sha'], directory_identity=value['directory_identity'],
            device=value['device'], hard_bytes=value['hard_bytes'], hard_inodes=64,
            check_unused=lambda project: pytest.fail('existing retry must not touch kernel'))


def test_collision_does_not_persist_an_intent(held):
    path, fd = held
    value = binding(fd, '1'*32)
    def reject(project):
        assert project == 1001
        raise BootstrapRejected('STATE_CONFLICT')
    with pytest.raises(BootstrapRejected, match='STATE_CONFLICT'):
        ledger.reserve(fd, value['candidate_id'], first_id=1001, last_id=1100,
            policy_sha=value['policy_sha'], directory_identity=value['directory_identity'],
            device=value['device'], hard_bytes=value['hard_bytes'], hard_inodes=64,
            check_unused=reject)
    assert list(path.iterdir()) == []


def test_repeat_preserves_binding_without_kernel_allocation(held):
    _, fd = held
    value = binding(fd, '1'*32)
    calls = []
    args = dict(first_id=1001, last_id=1100, policy_sha=value['policy_sha'],
                directory_identity=value['directory_identity'], device=value['device'],
                hard_bytes=value['hard_bytes'], hard_inodes=64, check_unused=calls.append)
    first = ledger.reserve(fd, value['candidate_id'], **args)
    assert ledger.reserve(fd, value['candidate_id'], **args) == first
    assert calls == [1001]
