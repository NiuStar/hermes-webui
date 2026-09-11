"""Registry evidence must use protected stable record reads."""
import os
import pytest
from api.display_bootstrap_manifest import canonical_bytes
from api.display_bootstrap_publish import read_registry


@pytest.mark.parametrize('mode', [0o622, 0o666])
def test_writable_registry_record_rejected(tmp_path, mode):
    raw=canonical_bytes(dict(format_version=1,candidate_id='a'*32,seq=1,
        previous_sha=None,state='RESERVED',target_name='a'*32,
        manifest_sha=None,approval_id=None,error_code=None))
    path=tmp_path/'00000000000000000001.json'
    path.write_bytes(raw)
    path.chmod(mode)
    fd=os.open(tmp_path,os.O_RDONLY|os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError,match='ACCESS_BOUNDARY_UNPROVEN'):
            read_registry(fd,'a'*32)
        assert path.read_bytes()==raw
    finally:
        os.close(fd)
