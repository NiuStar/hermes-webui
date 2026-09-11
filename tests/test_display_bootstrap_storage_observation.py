"""Negative schema matrix; these tests do not qualify live storage."""
import pytest
from api.display_bootstrap_storage_observation import mount, journal


def mount_record():
    return dict(mount_id=1, device=dict(major=7, minor=1), root='/',
                target='/audit', filesystem='ext4', options=['rw'])


@pytest.mark.parametrize('key,value', [
    ('mount_id', True), ('mount_id', 0), ('device', {'major': True, 'minor': 1}),
    ('options', ['rw', 'rw']), ('options', ['rw', 'ro']), ('options', []),
    ('root', '/subdir'), ('filesystem', 'overlay'), ('target', 'relative'),
])
def test_mount_rejects_bad_values(key, value):
    item = mount_record()
    item[key] = value
    with pytest.raises(ValueError):
        mount(item)


@pytest.mark.parametrize('key', list(mount_record()))
def test_mount_missing_fields(key):
    item = mount_record()
    del item[key]
    with pytest.raises(ValueError):
        mount(item)


def test_mount_unknown_field():
    item = mount_record()
    item['trusted'] = True
    with pytest.raises(ValueError):
        mount(item)


def test_valid_mount():
    mount(mount_record())


def test_journal_missing_fields():
    with pytest.raises(ValueError):
        journal({'internal_journal': True})
