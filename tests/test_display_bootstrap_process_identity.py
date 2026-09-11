"""Real process identity gates; no candidate authority is granted here."""
import json
import os
import subprocess
import sys

import pytest
from api import display_bootstrap_policy as policy


def test_root_cannot_impersonate_creator():
    with pytest.raises(policy.BootstrapRejected, match='ACCESS_BOUNDARY_UNPROVEN'):
        policy.verify_process_identity({'creator_uid': 999, 'approver_uid': 0})


def test_dedicated_unprivileged_process_is_verified():
    code = """
import json, os
from api.display_bootstrap_policy import verify_process_identity
print(json.dumps(verify_process_identity({'creator_uid': os.getuid(), 'approver_uid': 0})))
"""
    result = subprocess.run(['runuser', '-u', 'hermes-bootstrap-test', '--', sys.executable,
                             '-c', code], capture_output=True, text=True, check=True)
    actual = json.loads(result.stdout)
    assert actual['uid'] == 999
    assert actual['gid'] == 987
    assert actual['capabilities'] == 0


def test_extra_supplementary_group_is_rejected():
    code = """
import os
from api.display_bootstrap_policy import verify_process_identity, BootstrapRejected
os.setgroups([0,987]); os.setgid(987); os.setuid(999)
try:
    verify_process_identity({'creator_uid': 999, 'approver_uid': 0})
except BootstrapRejected as exc:
    assert exc.code == 'ACCESS_BOUNDARY_UNPROVEN'
else:
    raise AssertionError('extra group admitted')
"""
    subprocess.run([sys.executable, '-c', code], check=True)
