"""Pinned current-policy binding, with retained real filesystem records."""
import os
import pytest
from api import display_bootstrap_policy as policy
from api.display_bootstrap_manifest import canonical_bytes, digest


def test_active_policy_hash_and_id(tmp_path, monkeypatch):
    active = dict(format_version=1, policy_id='a'*32, deployment_policy_sha=digest(b'{"test":1}'))
    (tmp_path/'active.json').write_bytes(canonical_bytes(active))
    (tmp_path/'policies').mkdir()
    (tmp_path/'policies'/('a'*32+'.json')).write_bytes(b'{"test":1}')
    # Isolate schema validation only: binding still reads actual canonical bytes.
    monkeypatch.setattr(policy, '_validate_deployment', lambda value: value)
    fd = os.open(tmp_path, os.O_RDONLY|os.O_DIRECTORY)
    try:
        assert policy.read_active_policy(fd, 'a'*32)[0] == {'test':1}
        with pytest.raises(policy.BootstrapRejected, match='APPROVAL_MISMATCH'):
            policy.read_active_policy(fd, 'b'*32)
        (tmp_path/'policies'/('a'*32+'.json')).write_bytes(b'{"test":2}')
        with pytest.raises(policy.BootstrapRejected, match='APPROVAL_MISMATCH'):
            policy.read_active_policy(fd, 'a'*32)
    finally:
        os.close(fd)


def test_active_policy_symlink_directory(tmp_path):
    (tmp_path/'active.json').write_bytes(canonical_bytes(dict(format_version=1,policy_id='a'*32,deployment_policy_sha='b'*64)))
    (tmp_path/'real').mkdir()
    (tmp_path/'policies').symlink_to('real', target_is_directory=True)
    fd=os.open(tmp_path,os.O_RDONLY|os.O_DIRECTORY)
    try:
        with pytest.raises(policy.BootstrapRejected, match='ACCESS_BOUNDARY_UNPROVEN'):
            policy.read_active_policy(fd,'a'*32)
    finally:
        os.close(fd)
