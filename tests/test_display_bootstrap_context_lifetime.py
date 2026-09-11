"""Context ownership contract; private issuance is not platform attestation."""
import os
import pickle
import pytest
from api import display_bootstrap_policy as p


def test_context_rejects_public_construction():
    with pytest.raises(p.BootstrapRejected):
        p.BootstrapContext()


def test_context_owns_handles_and_rejects_closed_and_fork(tmp_path, monkeypatch):
    root = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    owned = os.dup(root)
    events = []
    class Lock:
        def close(self): events.append('unlock')
    import time
    from api import display_bootstrap_admission as admission
    from api import display_bootstrap_admission_ownership as ownership
    prepared = ownership._new_preparation(time.monotonic()+30)
    prepared.mode, prepared.role, prepared.started = 'CREATE', 'creator', time.monotonic()
    prepared.root_fd, prepared.lock, prepared.roots = root, Lock(), {'candidate_root': owned}
    prepared.owner.callback(os.close, root)
    prepared.owner.callback(prepared.lock.close)
    prepared.owner.callback(os.close, owned)
    prepared.config, prepared.resource, prepared.deployment_sha = {}, {}, 'a'*64
    prepared.storage, prepared.release = None, None
    prepared.candidate_id, prepared.audit_fd = 'b'*32, owned
    prepared.candidate_fd, prepared.candidate_identity = owned, {}
    prepared.boundary, prepared.audit_snapshot = {}, {}
    prepared.platform_evidence, prepared.observation = {}, {}
    # Mock only OS attestation; exercise the real receipt transfer and Context.
    monkeypatch.setattr(admission, 'verify_prepared', lambda *args: ())
    receipt = ownership._register_receipt(prepared, ())
    ctx = p.BootstrapContext._issue(receipt)
    prepared.close()
    assert events == []
    with pytest.raises(ValueError):
        p.BootstrapContext._issue(receipt)
    ctx.check_owner()
    with pytest.raises(TypeError): pickle.dumps(ctx)
    pid = os.fork()
    if pid == 0:
        try:
            ctx.check_owner()
        except p.BootstrapRejected:
            ctx.close()
            os._exit(0)
        os._exit(1)
    assert os.waitpid(pid, 0)[1] == 0
    ctx.check_owner()
    with ctx:
        assert ctx.roots['candidate_root'] == owned
    ctx.close()
    assert events == ['unlock']
    for fd in (root, owned):
        with pytest.raises(OSError): os.fstat(fd)
    with pytest.raises(p.BootstrapRejected): ctx.check_owner()
