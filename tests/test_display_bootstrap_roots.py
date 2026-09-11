"""Live directory identity checks; tests retain replacement directories."""
import os
import pytest
from api import display_bootstrap_policy as policy


def identity(path):
    st=path.stat()
    return {k:getattr(st,'st_'+k) for k in ('dev','ino','uid','gid','mode')}


def test_policy_roots_held_and_replacement_rejected(tmp_path):
    roots={}
    for key in ('registry_root','approval_root','candidate_root','publish_root','lock_root'):
        path=tmp_path/key
        path.mkdir(mode=0o700)
        roots[key]=dict(path=str(path),identity=identity(path))
    # Real paths/FDs, root test actor only; no context authority is granted.
    config=dict(roots=roots,ancestors=[],creator_uid=os.getuid(),approver_uid=os.getuid())
    fds=policy.open_policy_roots(config)
    try:
        assert set(fds)==set(roots)
        original=tmp_path/'candidate_root'
        original.rename(tmp_path/'retained-candidate-root')
        original.mkdir(mode=0o700)
        with pytest.raises(policy.BootstrapRejected,match='IDENTITY_CHANGED'):
            policy.recheck_policy_roots(config,fds)
    finally:
        for fd in fds.values(): os.close(fd)


def test_policy_root_failure_releases_fds(tmp_path):
    path=tmp_path/'root'
    path.mkdir(mode=0o700)
    value=identity(path)
    value['ino']+=1
    before=set(os.listdir('/proc/self/fd'))
    with pytest.raises(policy.BootstrapRejected,match='IDENTITY_CHANGED'):
        policy.open_policy_roots(dict(roots={'candidate_root':dict(path=str(path),identity=value)},ancestors=[],creator_uid=os.getuid(),approver_uid=os.getuid()))
    assert set(os.listdir('/proc/self/fd'))==before
