"""OS-backed trusted metadata admission; retain all test artifacts."""
import os
import pytest
from api import display_bootstrap_policy as policy


def test_read_protected_metadata(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    path = tmp_path / 'active.json'
    path.write_bytes(b'{"a":1}')
    path.chmod(0o600)
    try:
        assert policy.read_protected_record(fd, 'active.json', {os.getuid()}) == b'{"a":1}'
        path.chmod(0o666)
        with pytest.raises(policy.BootstrapRejected):
            policy.read_protected_record(fd, 'active.json', {os.getuid()})
    finally:
        os.close(fd)


@pytest.mark.parametrize('variant', ['symlink', 'hardlink', 'fifo', 'oversize', 'owner'])
def test_reject_untrusted_metadata(tmp_path, variant):
    path = tmp_path / 'active.json'
    trusted = {os.getuid()}
    if variant == 'symlink':
        (tmp_path / 'other').write_bytes(b'{}')
        path.symlink_to('other')
    elif variant == 'hardlink':
        (tmp_path / 'other').write_bytes(b'{}')
        os.link(tmp_path / 'other', path)
    elif variant == 'fifo':
        os.mkfifo(path)
    else:
        path.write_bytes(b'x' * 65537 if variant == 'oversize' else b'{}')
        if variant == 'owner':
            trusted = set()
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(policy.BootstrapRejected):
            policy.read_protected_record(fd, 'active.json', trusted)
    finally:
        os.close(fd)
