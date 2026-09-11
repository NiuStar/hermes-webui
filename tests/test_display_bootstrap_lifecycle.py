"""Legacy schemas are read-only; V3 lifecycle lives in test_display_bootstrap_v3_lifecycle.

Migrated coverage: durable chain/evidence, create failures and resource exhaustion,
quota/confinement ordering, retention, role separation, approval mismatch/rebind,
completion I/O recovery, conflict classification and shared deadline expiry.
Old per-record writable media is deliberately not re-enabled for these tests.
"""
import os
import pytest
from api import display_bootstrap_policy as policy
from api import display_bootstrap_lifecycle as lifecycle


@pytest.mark.parametrize('version',[1,2])
@pytest.mark.parametrize('operation',['create','publish','recover'])
def test_legacy_context_cannot_write(tmp_path,monkeypatch,version,operation):
    ctx=object.__new__(policy.BootstrapContext)
    ctx.pid,ctx.closed=os.getpid(),False
    ctx.config={'format_version':version}
    ctx.roots={}
    for key in ('candidate_root','registry_root','publish_root','approval_root'):
        path=tmp_path/key
        path.mkdir()
        ctx.roots[key]=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    monkeypatch.setattr(policy.BootstrapContext,'revalidate',lambda self:self.check_owner())
    try:
        if operation=='create':
            result=lifecycle.create_candidate(ctx)
        elif operation=='publish':
            result=lifecycle.publish_candidate(ctx,'a'*32,'b'*32)
        else:
            result=lifecycle.recover_candidate(ctx,'a'*32)
        assert result['status']=='BLOCKED',result
        assert result['code']=='ACCESS_BOUNDARY_UNPROVEN',result
        assert all(not list((tmp_path/key).iterdir()) for key in ctx.roots)
    finally:
        for fd in ctx.roots.values(): os.close(fd)
        ctx.closed=True


def test_invalid_context_is_blocked():
    result=lifecycle.create_candidate(None)
    assert result==dict(format_version=1,status='BLOCKED',code='ACCESS_BOUNDARY_UNPROVEN',
                        candidate_id=None,target_name=None,registry_seq=None)
    assert lifecycle.publish_candidate(None,'../bad',{})['code']=='INVALID_INPUT'
