"""Run the source identity probe inside an ephemeral, networkless Docker container."""
import shlex
import subprocess
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.docker_self_update import _AGENT_RUNTIME_PROBE

SSH = [
    'ssh', '-i', '/workspace/sshkey/id_rsa', '-o', 'IdentitiesOnly=yes',
    '-o', 'UserKnownHostsFile=/workspace/auction-module-recovery/known_hosts_builder',
    '-o', 'StrictHostKeyChecking=yes', '-o', 'BatchMode=yes', 'root@10.126.126.3',
]
parser = argparse.ArgumentParser()
parser.add_argument('--expected-revision', default='24fd22b94df040d843eb280ff197a4bcd99a6fc3')
parser.add_argument('--mutate-staged', action='store_true')
options = parser.parse_args()
revision = options.expected_revision
script = (
    'set -eu; cp -a /opt/hermes /tmp/probe-staged; '
    + ('printf tampered > /tmp/probe-staged/hermes_cli/__init__.py; ' if options.mutate_staged else '')
    + 'PYTHONPATH=/tmp/probe-staged /usr/local/bin/python -c '
    + shlex.quote(_AGENT_RUNTIME_PROBE)
    + ' ' + shlex.join([revision, '/opt/hermes', '/tmp/probe-staged'])
)
args = [
    'docker', 'run', '--rm', '--network', 'none',
    '--entrypoint', '/bin/sh', '24802117/hermes-webui:v2026.10.08-r1',
    '-c', script,
]
result = subprocess.run(SSH + [shlex.join(args)], capture_output=True, text=True, timeout=240)
print('probe_exit', result.returncode)
print('stdout_tail', result.stdout[-200:])
print('stderr_tail', result.stderr[-800:])
sys.exit(result.returncode)
