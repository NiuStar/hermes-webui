"""OS role separation probes under the dedicated test UID."""
import os
import subprocess
import sys
import pytest


@pytest.mark.parametrize('unsafe', [False, True])
def test_creator_cannot_write_approval_root(tmp_path, unsafe):
    paths = {}
    for key in ('candidate_root', 'publish_root', 'registry_root', 'approval_root', 'lock_root'):
        path = tmp_path / key
        path.mkdir(mode=0o755)
        if key not in ('approval_root', 'lock_root') or (key == 'approval_root' and unsafe):
            os.chown(path, 999, 987)
        paths[key] = str(path)
    # Test-only ancestors permit traversal, never grant candidate authority.
    tmp_path.chmod(0o755)
    tmp_path.parent.chmod(0o755)
    code = """
import os, sys
from api.display_bootstrap_policy import verify_role_permissions, BootstrapRejected
paths = __import__('json').loads(sys.argv[1])
fds = {k:os.open(v, os.O_RDONLY|os.O_DIRECTORY) for k,v in paths.items()}
try:
    try:
        verify_role_permissions(fds, {'creator_uid':999,'approver_uid':0})
    except BootstrapRejected as exc:
        assert sys.argv[2] == 'True' and exc.code == 'ACCESS_BOUNDARY_UNPROVEN'
    else:
        assert sys.argv[2] == 'False'
finally:
    for fd in fds.values(): os.close(fd)
"""
    result = subprocess.run(['runuser', '-u', 'hermes-bootstrap-test', '--', sys.executable,
                             '-c', code, __import__('json').dumps(paths), str(unsafe)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
