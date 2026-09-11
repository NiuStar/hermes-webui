"""Preparation ordering and cleanup; not positive OS admission evidence."""
import time
import pytest
from api import display_bootstrap_admission as admission
from api import display_bootstrap_policy as policy
from api import display_bootstrap_roles as roles
from api.display_bootstrap_manifest import canonical_bytes


@pytest.mark.parametrize('refuse_roles', [False, True])
def test_admission_checks_roles_under_lock_and_closes_roots(monkeypatch, refuse_roles):
    calls = []
    class Lock:
        def close(self): calls.append('unlock')
    anchor = canonical_bytes(dict(format_version=1,
        lock_path='/etc/hermes-display-bootstrap/bootstrap.lock',
        lock_identity=dict(dev=1, ino=2, uid=0, gid=0, mode=33152, nlink=1)))
    monkeypatch.setattr(admission, 'open_protected_root', lambda *a: 123)
    monkeypatch.setattr(admission, 'read_protected_record', lambda *a: anchor)
    monkeypatch.setattr(admission, 'acquire_fixed_lock', lambda *a: Lock())
    config = dict(format_version=3, creator_uid=999, approver_uid=0, platform_requirements={})
    monkeypatch.setattr(admission, 'read_active_policy', lambda *a: (config, 'a'*64))
    monkeypatch.setattr(admission, 'verify_anchor_binding', lambda *a: calls.append('anchor'))
    def resources(fd, c):
        assert fd == 123 and c is config
        calls.append('resources')
        return {'max_elapsed_seconds': 60}
    monkeypatch.setattr(admission, 'read_resource_policy', resources)
    monkeypatch.setattr(roles, 'verify_identity', lambda c, role: calls.append(('identity', role)))
    monkeypatch.setattr(admission, 'open_policy_roots', lambda c: {'candidate_root': 124})
    def dac(fds, c, *, role, before_confinement):
        assert c is config and fds == {'candidate_root': 124}
        assert role == 'creator' and before_confinement is True
        calls.append('roles')
        if refuse_roles:
            raise policy.BootstrapRejected('IDENTITY_CHANGED')
    monkeypatch.setattr(roles, 'verify_dac_configuration', dac)
    def platform(*args):
        calls.append('platform')
        raise policy.BootstrapRejected('APPROVAL_MISMATCH')
    monkeypatch.setattr(policy, 'verify_platform', platform)
    monkeypatch.setattr(policy.os, 'close', lambda fd: calls.append(('close', fd)))
    with pytest.raises(policy.BootstrapRejected, match=('IDENTITY_CHANGED' if refuse_roles else 'APPROVAL_MISMATCH')):
        admission._prepare('a'*32, 'creator', time.monotonic())
    expected = ['anchor', 'resources', ('identity', 'creator'), 'roles']
    if not refuse_roles:
        expected.append('platform')
    assert calls == expected + [('close', 124), 'unlock', ('close', 123)]
