"""Observe the actual held mount, not caller-supplied evidence."""
import os
import sqlite3
import pytest
from api import display_bootstrap_policy as p


def test_observe_held_mount_and_exact_policy(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        actual = p.observe_platform(fd)
        assert actual['kernel'] == os.uname().release
        assert actual['sqlite_version'] == sqlite3.sqlite_version
        assert actual['namespace_id'] == os.readlink('/proc/self/ns/mnt')
        assert actual['mount_id'] > 0
        assert actual['vfs'] == 'unix'
        p.verify_platform(fd, actual)
        for key, replacement in [('mount_id', actual['mount_id']+1), ('kernel','wrong'), ('vfs','unix-none')]:
            changed = dict(actual, **{key: replacement})
            with pytest.raises(p.BootstrapRejected, match='UNSUPPORTED_PLATFORM'):
                p.verify_platform(fd, changed)
    finally:
        os.close(fd)
