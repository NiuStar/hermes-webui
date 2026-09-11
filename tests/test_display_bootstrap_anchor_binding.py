"""A deployment must use the already-held immutable global anchor."""
import os
import pytest
from api import display_bootstrap_policy as p


def test_anchor_binding_matches_live_root_and_lock(tmp_path):
    root = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    lock = os.open(tmp_path/'bootstrap.lock', os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        identity = {k:getattr(os.fstat(lock),'st_'+k) for k in ('dev','ino','uid','gid','mode','nlink')}
        anchor = {'lock_identity': identity}
        config = {'lock_identity': identity, 'roots': {'lock_root': {
            'path':'/etc/hermes-display-bootstrap','identity':p.directory_identity(root)}}}
        p.verify_anchor_binding(root, anchor, config)
        config['lock_identity'] = dict(identity, ino=identity['ino']+1)
        with pytest.raises(p.BootstrapRejected, match='IDENTITY_CHANGED'):
            p.verify_anchor_binding(root, anchor, config)
        config['lock_identity'] = identity
        config['roots']['lock_root']['path'] = '/other'
        with pytest.raises(p.BootstrapRejected, match='IDENTITY_CHANGED'):
            p.verify_anchor_binding(root, anchor, config)
    finally:
        os.close(lock)
        os.close(root)
