"""Volume profile validation, not live quota qualification."""
import pytest
from api.display_bootstrap_policy import BootstrapRejected


def test_volume_profile_rejects_unbounded_or_shared_storage():
    from api.display_bootstrap_volume import validate_volume_profile
    with pytest.raises(BootstrapRejected):
        validate_volume_profile({'format_version': 1}, 'a'*32)


def test_live_observer_rejects_non_volume(tmp_path):
    import os
    from api.display_bootstrap_volume import observe_fixed_volume
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(BootstrapRejected) as caught:
            observe_fixed_volume(fd)
        assert caught.value.code == 'ACCESS_BOUNDARY_UNPROVEN'
    finally:
        os.close(fd)


def test_volume_profile_exact_schema():
    from api.display_bootstrap_volume import validate_volume_profile
    identity = dict(dev=1, ino=2, uid=0, gid=0, mode=33152, nlink=1)
    def volume(dev):
        return dict(device=dev, mount_id=10+dev, total_bytes=67108864,
                    image_path='/opt/isolated/image'+str(dev), image_identity=identity,
                    image_bytes=67108864)
    value = dict(format_version=1, hard_limit_profile_id='a'*32,
                 candidate=volume(1), audit=volume(2))
    assert validate_volume_profile(value, 'a'*32) == value
    value['audit']['device'] = 1
    with pytest.raises(BootstrapRejected):
        validate_volume_profile(value, 'a'*32)
