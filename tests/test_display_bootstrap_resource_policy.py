"""Resource policy bytes are tied to the trusted deployment hash."""
import os
import pytest
from api import display_bootstrap_policy as policy
from api.display_bootstrap_manifest import canonical_bytes,digest


def test_resource_digest_binding(tmp_path):
    value=dict(format_version=1,max_candidate_bytes=1048576,min_free_bytes=1048576,
               max_retained_candidates=8,max_rss_bytes=67108864,max_elapsed_seconds=10,
               check_interval_ms=100,audit_reserve_bytes=1048576,hard_limit_profile_id='c'*32)
    raw=canonical_bytes(value)
    resources=tmp_path/'policies'/'resources'
    resources.mkdir(parents=True)
    path=resources/('a'*32+'.json')
    path.write_bytes(raw)
    config=dict(resource_policy_id='a'*32,resource_policy_sha=digest(raw))
    fd=os.open(tmp_path,os.O_RDONLY|os.O_DIRECTORY)
    try:
        assert policy.read_resource_policy(fd,config)==value
        path.write_bytes(canonical_bytes(dict(value,max_rss_bytes=134217728)))
        with pytest.raises(policy.BootstrapRejected,match='APPROVAL_MISMATCH'):
            policy.read_resource_policy(fd,config)
    finally:
        os.close(fd)
