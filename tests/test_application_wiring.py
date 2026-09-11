"""Wiring checks; fixture stubs are not tool-release acceptance evidence."""
import dataclasses
import json
import os
import pwd
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.test_application_v2 import runtime, _id, _request, ROOT
from api.application_broker import ApplicationBroker, client
from api.application_commands import dispatch
from api.application_fixed_tool import FixedPolicyTool
from api.application_operation_issue import ApplicationIssue
from api.application_protocol import parse_request
from api.application_task_service import ApplicationService
from scripts.application_task_entry import parser, exit_status


def test_explicit_cli_operations_and_exit_mapping():
    args = parser().parse_args(['--config', '/trusted/config', 'recover', '--task-id', _id(1),
        '--candidate-id', _id(2), '--interrupted-task-id', _id(3), '--expected-evidence-sha', 'a'*64,
        '--decision', 'close_failed'])
    assert args.approval_id is None
    for code, status in [('BUSY', 4), ('PERMISSION_DENIED', 3), ('INVALID_REQUEST', 2),
                          ('OUTCOME_UNKNOWN', 5), ('INTEGRITY_ERROR', 5)]:
        assert exit_status({'issue': {'code': code}}) == status
    with pytest.raises(ApplicationIssue):
        parser().parse_args(['--config', '/trusted/config', 'create'])


def test_protocol_invalid_shapes_are_classified():
    for raw in ('null', '[]', '1', '{}'):
        with pytest.raises(ApplicationIssue) as error:
            parse_request(raw)
        assert error.value.code == 'INVALID_REQUEST'
    for operation, params in [('publish', {'candidate_id': _id(1), 'approval_id': None}),
                              ([], {'candidate_id': _id(1)})]:
        with pytest.raises(ApplicationIssue):
            parse_request(_request(_id(2), operation, params))


def test_unconfigured_tool_does_not_block_reads_or_default_create(runtime):
    service, config = runtime
    broken = ApplicationService(dataclasses.replace(config, fixed_tool={'invalid': True}))
    assert broken.list(config.principal('creator'))['tasks'] == []
    with pytest.raises(ApplicationIssue):
        broken.submit(_request(_id(10), 'create', {'candidate_id': _id(11)}), config.principal('creator'))
    assert service.list(config.principal('creator'))['tasks'] == []
    result = service.submit(_request(_id(10), 'create', {'candidate_id': _id(11)}), config.principal('creator'))
    assert result['task_state'] == 'SUCCEEDED'


def test_tool_dispatch_after_durable_intent_without_sql_lock(runtime):
    service, config = runtime
    service = ApplicationService(dataclasses.replace(config, fixed_tool={'test_only': True}))
    called = []
    def run(tool, task_id, files, intent):
        with service.ledger.session(readonly=True) as db:
            assert db.execute('SELECT state FROM tasks WHERE id=?', (task_id,)).fetchone()[0] == 'RUNNING'
            assert db.execute('SELECT step FROM operation_intents WHERE request_id=?', (task_id,)).fetchone()[0] == 'create_artifact'
        import sqlite3
        with sqlite3.connect(config.ledger_path, timeout=0) as db:
            db.execute('BEGIN IMMEDIATE')
            db.rollback()
        called.append(task_id)
        return {'tool_record_sha': 'b'*64, 'tool_release_sha': 'c'*64}
    with patch.object(FixedPolicyTool, 'preflight'), patch.object(FixedPolicyTool, 'run', run):
        request = _request(_id(20), 'create', {'candidate_id': _id(21)})
        result = service.submit(request, config.principal('creator'))
        assert result['task_state'] == 'SUCCEEDED'
        assert service.submit(request, config.principal('creator')) == result
    assert called == [_id(20)]


def test_fixed_policy_script_real_execution():
    command = [sys.executable, '-I', '-B', str(ROOT/'scripts/application_fixed_policy_test.py')]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output['verdict'] == 'PASS' and output['tests_run'] > 0
    rejected = subprocess.run(command+['--argv-injection'], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert rejected.returncode != 0


def test_broker_real_peer_and_strict_dispatch(runtime, monkeypatch):
    _, config = runtime
    name = pwd.getpwuid(os.geteuid()).pw_name
    config = dataclasses.replace(config, principals={name: {'permissions': ['create'], 'task_read_all': False}})
    socket_path = config.ledger_path.parent/'cli.sock'
    import socket
    from tests.conftest import _REAL_SOCKET_CONNECT
    original_connect = socket.socket.connect
    def connect(connection, address):
        if connection.family == socket.AF_UNIX and address == str(socket_path):
            return _REAL_SOCKET_CONNECT(connection, address)
        return original_connect(connection, address)
    monkeypatch.setattr(socket.socket, 'connect', connect)
    with ApplicationBroker(config, socket_path) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            created = client(socket_path, os.geteuid(), {'action': 'submit', 'arguments': {
                'request': _request(_id(31), 'create', {'candidate_id': _id(32)})}})
            assert created['task_state'] == 'SUCCEEDED'
            denied = client(socket_path, os.geteuid(), {'action': 'list', 'arguments': {
                'limit': 20, 'offset': 0}, 'principal': 'approver'})
            assert denied['issue']['code'] == 'INVALID_REQUEST'
            with pytest.raises(ApplicationIssue):
                client(socket_path, os.geteuid()+1, {'action': 'list', 'arguments': {'limit': 20, 'offset': 0}})
        finally:
            server.shutdown()
            thread.join(timeout=5)
    assert not socket_path.exists()


def test_list_limits_and_unknown_commands(runtime):
    service, config = runtime
    for args in ({'limit': 0, 'offset': 0}, {'limit': 10, 'offset': -1}):
        with pytest.raises(ApplicationIssue):
            dispatch(service, config.principal('creator'), {'action': 'list', 'arguments': args})
    with pytest.raises(ApplicationIssue):
        dispatch(service, config.principal('creator'), {'action': 'shell', 'arguments': {}})


def test_http_pending_and_recovery_route_contract(runtime):
    from api import application_routes as routes
    from api.application_operation_issue import envelope, issue
    from urllib.parse import urlparse
    seen = []
    with patch.object(routes, 'j', lambda handler, data, status=200: seen.append((status, data))):
        routes._send(None, lambda: envelope(_id(1), accepted=True, task_state='RUNNING',
                                           problem=issue('OUTCOME_UNKNOWN')))
        assert seen[-1][0] == 202
        routes.handle_application_post(None, urlparse('/api/application/tasks/'+_id(2)+'/recovery'),
            _request(_id(3), 'create', {'candidate_id': _id(4), 'interrupted_task_id': _id(2)}))
        assert seen[-1][0] == 409
