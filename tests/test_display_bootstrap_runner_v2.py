"""Explicit V2 runner identity schema and worker rendering."""
import copy
import pytest
from api.display_bootstrap_runner import _validate_profile
from api.display_bootstrap_worker_units import render_worker_unit


def profile():
    return dict(format_version=2,hard_limit_profile_id='a'*32,
                unit='hermes-bootstrap-worker.service',role='creator',uid=1001,gid=1001,
                supplementary_gids=[],memory_max_bytes=67108864,memory_swap_max_bytes=0,
                pids_max=8,runtime_max_usec=60000000,mount_namespace='mnt:[1]',
                cgroup_namespace='cgroup:[2]',user_namespace='user:[3]')


def test_v2_preserves_input():
    value=profile()
    before=copy.deepcopy(value)
    assert _validate_profile(value,'a'*32) is value
    assert value == before


@pytest.mark.parametrize('changes',[dict(uid=0),dict(gid=True),dict(creator_uid=1001),
    dict(role='unknown'),dict(supplementary_gids=[9999]),dict(role='approver'),
    dict(supplementary_gids=[1001])])
def test_invalid_v2(changes):
    with pytest.raises(ValueError):
        _validate_profile(dict(profile(),**changes),'a'*32)


def test_worker_role_mismatch_rejected():
    with pytest.raises(ValueError):
        render_worker_unit(release_path='/opt/bootstrap',profile=profile(),policy_id='b'*32,
                           operation='publish',candidate_id='c'*32,approval_id='d'*32)


def test_approval_unit_has_separate_identity_and_explicit_reference():
    value = dict(profile(), role='approver', uid=1002, gid=1002, supplementary_gids=[1001])
    text = render_worker_unit(release_path='/opt/bootstrap', profile=value, policy_id='b'*32,
        operation='approve', candidate_id='c'*32, approval_id='d'*32,
        manifest_sha='e'*64, policy_sha='f'*64, reference='review $HOME %n "ok"')[value['unit']]
    assert 'User=1002\nGroup=1002\nSupplementaryGroups=1001\n' in text
    assert 'UMask=0027\n' in text
    assert 'bootstrap_worker_entry.py approve --policy-id' in text
    assert '$$HOME %%n' in text
    assert 'Requires=' not in text


def test_approval_unit_rejects_newline_injection():
    value = dict(profile(), role='approver', uid=1002, gid=1002, supplementary_gids=[1001])
    with pytest.raises(ValueError):
        render_worker_unit(release_path='/opt/bootstrap', profile=value, policy_id='b'*32,
            operation='approve', candidate_id='c'*32, approval_id='d'*32,
            manifest_sha='e'*64, policy_sha='f'*64, reference='review\nUser=root')


def test_worker_uses_v2_identity():
    units=render_worker_unit(release_path='/opt/bootstrap',profile=profile(),policy_id='b'*32,
                             operation='create', candidate_id='c'*32)
    text=units['hermes-bootstrap-worker.service']
    assert 'User=1001\nGroup=1001\nSupplementaryGroups=\n' in text
