"""Missing privileged broker must block; no simulated server success."""
import subprocess
import sys


def test_missing_broker_blocks_guarded_worker():
    code = '''
import os
import time
from api.display_bootstrap_seccomp import install_quota_guard
from api.display_bootstrap_quota import candidate_quota
from api.display_bootstrap_policy import BootstrapRejected
class Context:
    deployment_sha='1'*64
    resource={'max_candidate_bytes':8388608}
    config={}
    role='creator'
    def operation_deadline(self): return time.monotonic()+10
    def check_owner(self): pass
install_quota_guard()
fd=os.open('/opt/hermes-bootstrap-tests/lifecycle-01',os.O_RDONLY|os.O_DIRECTORY)
try:
    try: candidate_quota(Context(),'a'*32,fd,allocate=True)
    except BootstrapRejected as e: assert e.code=='ACCESS_BOUNDARY_UNPROVEN'
    else: raise AssertionError('missing broker accepted')
finally: os.close(fd)
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
