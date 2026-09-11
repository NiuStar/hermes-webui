"""Admission must validate parent before opening anchor content."""
import pytest
from api import display_bootstrap_policy as policy
from api import display_bootstrap_admission as admission
import time


def test_admission_reads_bound_policy_and_releases(monkeypatch):
    from api.display_bootstrap_manifest import canonical_bytes
    calls = []
    class Lock:
        def close(self):
            calls.append('unlock')
    monkeypatch.setattr(admission, 'open_protected_root', lambda *args: 123)
    identity = dict(dev=1,ino=2,uid=0,gid=0,mode=33152,nlink=1)
    anchor = canonical_bytes(dict(format_version=1,lock_path='/etc/hermes-display-bootstrap/bootstrap.lock',lock_identity=identity))
    monkeypatch.setattr(admission, 'read_protected_record', lambda *args: anchor)
    monkeypatch.setattr(admission, 'acquire_fixed_lock', lambda *args: Lock())
    def refuse(fd, requested):
        calls.append(('policy', fd, requested))
        raise policy.BootstrapRejected('APPROVAL_MISMATCH')
    monkeypatch.setattr(admission, 'read_active_policy', refuse)
    monkeypatch.setattr(policy.os, 'close', lambda fd: calls.append(('close',fd)))
    with pytest.raises(policy.BootstrapRejected, match='APPROVAL_MISMATCH'):
        admission._prepare('a'*32, 'creator', time.monotonic())
    assert calls == [('policy',123,'a'*32),'unlock',('close',123)]


def test_untrusted_parent_stops_anchor_read(monkeypatch):
    calls = []
    def reject(path, uids):
        calls.append((path, uids))
        raise policy.BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
    monkeypatch.setattr(admission, 'open_protected_root', reject)
    with pytest.raises(policy.BootstrapRejected, match='ACCESS_BOUNDARY_UNPROVEN'):
        admission._prepare('a'*32, 'creator', time.monotonic())
    assert calls == [('/etc/hermes-display-bootstrap', {0})]
