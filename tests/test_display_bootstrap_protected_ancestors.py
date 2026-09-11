"""Protected roots cannot live under creator-owned ancestors."""
import os
import pytest
from api import display_bootstrap_policy as p


def test_creator_owned_approval_ancestor_rejected(tmp_path):
    parent=tmp_path/'creator-owned'
    parent.mkdir(mode=0o755)
    os.chown(parent,999,987)
    approval=parent/'approval'
    approval.mkdir(mode=0o755)
    config={'creator_uid':999,'approver_uid':0,'ancestors':[],
            'roots':{'approval_root':{'path':str(approval),'identity':dict(
                (k,getattr(approval.stat(),'st_'+k)) for k in ('dev','ino','uid','gid','mode'))}}}
    with pytest.raises(p.BootstrapRejected,match='ACCESS_BOUNDARY_UNPROVEN'):
        fds=p.open_policy_roots(config)
        for fd in fds.values(): os.close(fd)
