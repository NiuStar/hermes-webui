"""Approval command protocol tests; no OS qualification claims."""
import json
import pytest
from api import display_bootstrap_approval_command as command
from api.display_bootstrap_approver import ApprovalUncertain


def args():
    return ['--policy-id', 'a'*32, '--candidate-id', 'b'*32,
            '--approval-id', 'c'*32, '--manifest-sha', 'd'*64,
            '--policy-sha', 'e'*64, '--reference', 'external review 42']


def test_explicit_record_forwarded(monkeypatch, capsys):
    observed = []
    def issue(policy_id, record):
        observed.append((policy_id, record))
        return dict(status='APPROVAL_DURABLE', approval_id=record['approval_id'], approval_sha='f'*64)
    monkeypatch.setattr(command, 'issue', issue)
    assert command.main(args()) == 0
    assert observed[0][0] == 'a'*32
    assert observed[0][1]['target_name'] == 'b'*32
    assert observed[0][1]['approver_reference'] == 'external review 42'
    assert json.loads(capsys.readouterr().out)['status'] == 'APPROVAL_DURABLE'


@pytest.mark.parametrize('field,value', [('--policy-id', 'A'*32), ('--manifest-sha', 'd'*63),
                                        ('--reference', ' ')])
def test_invalid_input_never_enters_issue(monkeypatch, field, value):
    def forbidden(*args):
        pytest.fail('invalid input reached authority')
    monkeypatch.setattr(command, 'issue', forbidden)
    argv = args()
    argv[argv.index(field)+1] = value
    with pytest.raises(SystemExit) as result:
        command.main(argv)
    assert result.value.code == 2


def test_rename_uncertainty_is_not_blocked(monkeypatch, capsys):
    def uncertain(*args):
        raise ApprovalUncertain('not durable')
    monkeypatch.setattr(command, 'issue', uncertain)
    assert command.main(args()) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'UNCERTAIN'
