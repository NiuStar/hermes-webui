"""Unit rendering contracts only; no systemd admission is established."""
import pytest
from api.display_bootstrap_worker_units import render_worker_unit
from api.display_bootstrap_policy import BootstrapRejected


@pytest.fixture
def arguments():
    return dict(release_path='/opt/bootstrap/releases/one', policy_id='a'*32,
                operation='create', candidate_id='c'*32, profile=dict(
                    format_version=1, hard_limit_profile_id='b'*32,
                    unit='hermes-bootstrap-worker.service', creator_uid=1234,
                    creator_gid=1234, memory_max_bytes=268435456,
                    memory_swap_max_bytes=0, pids_max=16, runtime_max_usec=60000000,
                    mount_namespace='mnt:[1]', cgroup_namespace='cgroup:[2]',
                    user_namespace='user:[3]'))


@pytest.mark.parametrize('operation', ['create', 'publish', 'recover'])
def test_operation_generation(arguments, operation):
    arguments['operation'] = operation
    if operation != 'create':
        arguments['candidate_id'] = 'c'*32
    if operation == 'publish':
        arguments['approval_id'] = 'd'*32
    result = render_worker_unit(**arguments)
    assert set(result) == {'hermes-bootstrap-worker.service'}
    text = next(iter(result.values()))
    assert 'Type=exec\n' in text
    assert 'RuntimeMaxSec=60000000us\n' in text
    assert 'MemoryMax=268435456\nMemorySwapMax=0\n' in text
    assert 'Restart=no\n' in text
    assert 'KillSignal=SIGKILL\n' in text
    assert 'FinalKillSignal=SIGKILL\n' in text
    assert 'KillMode=control-group\n' in text
    assert 'Delegate=no\n' in text
    assert ' -I -B -S ' in text
    assert f"--policy-id {'a'*32} {operation}" in text
    assert '--candidate-id' in text
    assert ('--approval-id' in text) == (operation == 'publish')
    assert '[Install]' not in text


@pytest.mark.parametrize('changes', [
    {'release_path': '/opt/a/../b'}, {'release_path': '/opt/a\nExecStart=bad'},
    {'release_path': '/opt/%n'}, {'policy_id': 'A'*32},
    {'operation': 'activate'}, {'candidate_id': None},
    {'operation': 'publish'}, {'operation': 'recover', 'candidate_id': '../bad'},
    {'operation': 'recover', 'candidate_id': 'c'*32, 'approval_id': 'd'*32},
])
def test_invalid_inputs_rejected(arguments, changes):
    arguments.update(changes)
    with pytest.raises(BootstrapRejected, match='INVALID_INPUT'):
        render_worker_unit(**arguments)


def test_root_worker_rejected(arguments):
    arguments['profile']['creator_uid'] = 0
    with pytest.raises(BootstrapRejected, match='INVALID_INPUT'):
        render_worker_unit(**arguments)
