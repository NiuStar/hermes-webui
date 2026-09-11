import pytest
from api.display_bootstrap_storage_topology import parse_mounts, _unescape


def test_mountinfo_device_and_escaped_target():
    result = parse_mounts('31 20 7:1 / /audit\\040volume rw,noatime - ext4 /dev/loop1 rw,data=ordered\n')
    assert result[31]['target'] == '/audit volume'
    assert result[31]['device'] == {'major': 7, 'minor': 1}
    assert result[31]['options'] == ['data=ordered', 'noatime', 'rw']


@pytest.mark.parametrize('text', [
    '31 20 7:1 / / rw - ext4 /dev/loop1',
    '0 20 7:1 / / rw - ext4 /dev/loop1 rw',
    '31 20 -7:1 / / rw - ext4 /dev/loop1 rw',
    '31 20 7:1 / / rw - ext4 /dev/loop1 rw\n31 20 7:1 / / rw - ext4 /dev/loop1 rw',
])
def test_invalid_mount_records(text):
    with pytest.raises(ValueError):
        parse_mounts(text)


@pytest.mark.parametrize('value', [r'/bad\999', r'/bad\001', '/bad\\'])
def test_invalid_escape(value):
    with pytest.raises(ValueError):
        _unescape(value)
