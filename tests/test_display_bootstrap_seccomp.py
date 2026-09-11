"""Real subprocess seccomp tests; no filter installed in pytest itself."""
import subprocess
import sys


def run(code):
    return subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=15)


def test_uninstalled_guard_rejected():
    result = run('''from api.display_bootstrap_seccomp import verify_quota_guard
try: verify_quota_guard()
except RuntimeError as e: assert str(e)=='ACCESS_BOUNDARY_UNPROVEN'
else: raise AssertionError('unprotected worker accepted')
''')
    assert result.returncode == 0, result.stderr


def test_installed_guard_survives_exec_and_sqlite():
    result = run('''from api.display_bootstrap_seccomp import install_quota_guard,verify_quota_guard
import subprocess,sys,sqlite3
install_quota_guard()
assert verify_quota_guard()['quota_guard']=='seccomp-v1'
db=sqlite3.connect(':memory:');db.execute('create table proof(x)');db.close()
r=subprocess.run([sys.executable,'-c',"from api.display_bootstrap_seccomp import verify_quota_guard; assert verify_quota_guard()['no_new_privileges']"])
assert r.returncode==0
''')
    assert result.returncode == 0, result.stderr
