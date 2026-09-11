"""Volume binding unit coverage, not live filesystem qualification."""
import copy
import pytest
from api import display_bootstrap_volume as volume
from api.display_bootstrap_policy import BootstrapRejected


@pytest.fixture
def bound(monkeypatch):
    def image(device):
        return dict(device=device, mount_id=device + 10, total_bytes=67108864,
                    image_path=f'/opt/isolated/{device}.img', image_bytes=67108864,
                    image_identity=dict(dev=9, ino=device, uid=0, gid=0,
                                        mode=33152, nlink=1))
    profile = dict(format_version=1, hard_limit_profile_id='a'*32,
                   candidate=image(1), audit=image(2))
    roots = dict(candidate_root=10, publish_root=11, registry_root=12)
    actual = {10: copy.deepcopy(profile['candidate']),
              11: copy.deepcopy(profile['candidate']), 12: copy.deepcopy(profile['audit'])}
    monkeypatch.setattr(volume, 'read_volume_profile', lambda _: (profile, 'b'*64))
    monkeypatch.setattr(volume, 'observe_fixed_volume', lambda fd: actual[fd])
    return roots, profile, actual


def test_bound_volumes_return_observations_not_context(bound):
    roots, profile, _ = bound
    result = volume.verify_fixed_volumes(roots, {'hard_limit_profile_id': 'a'*32})
    assert result == dict(profile_sha='b'*64, candidate=profile['candidate'], audit=profile['audit'])


@pytest.mark.parametrize('root', [10, 11, 12])
def test_each_mount_is_checked(bound, root):
    roots, _, actual = bound
    actual[root]['mount_id'] += 1
    with pytest.raises(BootstrapRejected, match='IDENTITY_CHANGED'):
        volume.verify_fixed_volumes(roots, {'hard_limit_profile_id': 'a'*32})


def test_replaced_profile_rejected(bound, monkeypatch):
    roots, profile, _ = bound
    reads = iter([(profile, 'b'*64), (profile, 'c'*64)])
    monkeypatch.setattr(volume, 'read_volume_profile', lambda _: next(reads))
    with pytest.raises(BootstrapRejected, match='IDENTITY_CHANGED'):
        volume.verify_fixed_volumes(roots, {'hard_limit_profile_id': 'a'*32})


def test_same_backing_inode_rejected(bound):
    roots, profile, _ = bound
    profile['audit']['image_identity'] = copy.deepcopy(profile['candidate']['image_identity'])
    with pytest.raises(BootstrapRejected, match='IDENTITY_CHANGED'):
        volume.verify_fixed_volumes(roots, {'hard_limit_profile_id': 'a'*32})


@pytest.mark.parametrize('identifier', ['../profile', 'A'*32, '', None])
def test_invalid_id_never_opens_profile(monkeypatch, identifier):
    from api import display_bootstrap_policy
    def forbidden(*args):
        pytest.fail('invalid identifier reached filesystem')
    monkeypatch.setattr(display_bootstrap_policy, 'open_protected_root', forbidden)
    with pytest.raises(BootstrapRejected, match='INVALID_INPUT'):
        volume.read_volume_profile(identifier)
