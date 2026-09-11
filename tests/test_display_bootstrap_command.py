"""CLI dispatch and output boundary only; mocked context is not admission."""
from contextlib import contextmanager
import pytest
from api import display_bootstrap_command as command
from api.display_bootstrap_manifest import parse_record


@pytest.fixture
def dispatch(monkeypatch):
    calls = []
    from api import display_bootstrap_admission as admission
    def execute(policy_id, operation, candidate_id=None, approval_id=None):
        calls.append(('execute', policy_id, operation, candidate_id, approval_id))
        try:
            return command.create_candidate(None)
        finally:
            calls.append(('close',))
    monkeypatch.setattr(admission, 'execute_operation', execute)
    return calls


def test_invalid_success_has_no_stdout(dispatch, monkeypatch, capsys):
    monkeypatch.setattr(command, 'create_candidate', lambda _: {'status': 'VERIFIED'})
    assert command.main(['--policy-id', 'a'*32, 'create', '--candidate-id', 'b'*32]) == 2
    out = capsys.readouterr()
    assert out.out == ''
    assert 'Invalid lifecycle result' in out.err
    assert dispatch[-1] == ('close',)


def test_blocked_is_nonzero_and_canonical(dispatch, monkeypatch, capsys):
    def reject(_):
        raise command.BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
    monkeypatch.setattr(command, 'create_candidate', reject)
    assert command.main(['--policy-id', 'a'*32, 'create', '--candidate-id', 'b'*32]) == 1
    result = parse_record(capsys.readouterr().out.rstrip('\n').encode())
    assert result['status'] == 'BLOCKED'
    assert result['code'] == 'ACCESS_BOUNDARY_UNPROVEN'
    assert dispatch[-1] == ('close',)


def test_invalid_identifier_never_acquires(dispatch):
    with pytest.raises(SystemExit) as error:
        command.main(['--policy-id', 'A'*32, 'create'])
    assert error.value.code == 2
    assert dispatch == []


def test_valid_creation_output(dispatch, monkeypatch, capsys):
    result = dict(format_version=1, status='VERIFIED', code='OK',
                  candidate_id='b'*32, target_name='b'*32, registry_seq=3,
                  manifest_sha='c'*64)
    monkeypatch.setattr(command, 'create_candidate', lambda _: result)
    assert command.main(['--policy-id', 'a'*32, 'create', '--candidate-id', 'b'*32]) == 0
    assert parse_record(capsys.readouterr().out.rstrip('\n').encode()) == result
    assert dispatch[-1] == ('close',)


def test_missing_candidate_never_acquires(dispatch):
    with pytest.raises(SystemExit) as error:
        command.main(['--policy-id', 'a'*32, 'create'])
    assert error.value.code == 2
    assert dispatch == []
