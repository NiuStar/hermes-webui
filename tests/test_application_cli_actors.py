"""Linux multi-OS-identity integration. Only a caller-owned isolated directory.

Requires root solely to drop test children to existing UIDs; creates no accounts,
services or groups. All business calls execute non-root through the real CLI.
"""
import json
import os
import pwd
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from api.application_protocol import digest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.geteuid() != 0, reason='needs privilege only for isolated UID-switch test children')
def test_real_cli_multi_identity_lifecycle():
    base = os.environ.get('APPLICATION_BROKER_TEST_ROOT')
    if not base:
        pytest.skip('explicit isolated multi-identity directory required')
    root = Path(tempfile.mkdtemp(prefix='actors-', dir=base))
    root.chmod(0o755)
    service_user = pwd.getpwnam('nobody')
    actors = {name: pwd.getpwnam(name) for name in ('daemon', 'www-data', 'games')}
    state = root/'service'
    state.mkdir(mode=0o711)
    os.chown(state, service_user.pw_uid, service_user.pw_gid)
    policy = {'format': 'wiring-test-v2'}
    config = {'runtime_format': 'application_runtime_v2', 'ledger_path': str(state/'ledger.sqlite'),
        'artifact_root': str(state/'artifacts'), 'artifact_policy': policy,
        'creator_commit': 'wiring-test-only', 'principals': {
            'daemon': {'permissions': ['create'], 'task_read_all': False},
            'www-data': {'permissions': ['approve'], 'task_read_all': True},
            'games': {'permissions': ['publish', 'recover'], 'task_read_all': True}},
        'cli_socket': str(state/'cli.sock'), 'cli_broker_uid': service_user.pw_uid}
    config_path = root/'config.json'
    config_path.write_text(json.dumps(config))
    config_path.chmod(0o644)
    command = [sys.executable, '-m', 'scripts.application_task_entry', '--config', str(config_path)]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(ROOT))
    def drop(account):
        def apply():
            os.setgroups([service_user.pw_gid])
            os.setgid(account.pw_gid)
            os.setuid(account.pw_uid)
        return apply
    # The broker's primary group is the socket group. Only test child credentials
    # include that group; /etc/group and the users' normal access are unchanged.
    broker = subprocess.Popen(command+['serve'], cwd=ROOT, env=env,
        preexec_fn=drop(service_user), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    def call(actor, *args, expected=0):
        result = subprocess.run(command+list(args), cwd=ROOT, env=env,
            preexec_fn=drop(actors[actor]), capture_output=True, text=True, timeout=40)
        assert result.returncode == expected, (result.stdout, result.stderr)
        return json.loads(result.stdout)
    try:
        deadline = time.monotonic()+10
        while not (state/'cli.sock').exists():
            if broker.poll() is not None:
                raise AssertionError(broker.communicate())
            if time.monotonic() >= deadline:
                raise AssertionError('broker did not bind')
            time.sleep(0.02)
        assert call('daemon', 'init')['status'] == 'INITIALIZED'
        cid, create_id, approve_id, approval_id, publish_id = (f'{n:032x}' for n in range(1,6))
        created = call('daemon', 'create', '--task-id', create_id, '--candidate-id', cid)
        assert created['task_state'] == 'SUCCEEDED'
        assert call('daemon', 'create', '--task-id', create_id, '--candidate-id', cid) == created
        approval_args = ['approve', '--task-id', approve_id, '--candidate-id', cid,
            '--approval-id', approval_id, '--manifest-sha', created['result']['manifest_sha'],
            '--policy-sha', digest(policy), '--reference', 'isolated multi-identity integration']
        assert call('daemon', *approval_args, expected=3)['issue']['code'] == 'PERMISSION_DENIED'
        approved = call('www-data', *approval_args)
        assert approved['task_state'] == 'SUCCEEDED'
        published = call('games', 'publish', '--task-id', publish_id, '--candidate-id', cid,
                         '--approval-id', approval_id)
        assert published['result']['business_status'] == 'PUBLISHED_UNACTIVATED'
        assert call('games', 'get', '--task-id', publish_id) == published
        assert call('daemon', 'get', '--task-id', approve_id, expected=3)['issue']['code'] == 'NOT_FOUND'
        assert call('daemon', '--principal', 'www-data', 'list', expected=3)['issue']['code'] == 'PERMISSION_DENIED'
        tasks = call('games', 'list', '--limit', '10', '--offset', '0')['tasks']
        assert len(tasks) == 3
        with open(root/'evidence.json', 'w') as out:
            json.dump({'created': created, 'approved': approved, 'published': published,
                       'tasks': tasks, 'test_only': True}, out, indent=2)
    finally:
        # Only our own Popen handle; no host scans or service modifications.
        broker.send_signal(signal.SIGINT)
        try:
            broker.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            broker.kill()
            broker.communicate(timeout=5)
