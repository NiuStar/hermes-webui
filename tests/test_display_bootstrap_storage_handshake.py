"""Pure protocol contracts; not a systemd or storage qualification."""
import pytest
from api.display_bootstrap_storage_handshake import validate_binding, frame, expect


def binding():
    return dict(deployment_sha='a'*64, resource_sha='b'*64, role='creator',
        runner_profile_id='c'*32, runner_profile_sha='d'*64,
        volume_profile_id='e'*32, volume_profile_sha='f'*64,
        observer_profile_id='1'*32, observer_profile_sha='2'*64)


@pytest.mark.parametrize('role', ['approver', 'root', '', None])
def test_unauthorized_role(role):
    with pytest.raises(ValueError):
        validate_binding(dict(binding(), role=role))


def test_nonce_replay():
    value = frame('READY', binding(), '3'*32, '4'*32)
    with pytest.raises(ValueError, match='APPROVAL_MISMATCH'):
        expect(value, 'READY', binding(), '5'*32, '4'*32)


@pytest.mark.parametrize('field', ['version', 'deployment_sha', 'runner_profile_sha'])
def test_wrong_binding(field):
    value = frame('READY', binding(), '3'*32, '4'*32)
    value[field] = True
    with pytest.raises(ValueError):
        expect(value, 'READY', binding(), '3'*32, '4'*32)


def test_extra_field():
    value = frame('READY', binding(), '3'*32, '4'*32, trusted=True)
    with pytest.raises(ValueError):
        expect(value, 'READY', binding(), '3'*32, '4'*32)
