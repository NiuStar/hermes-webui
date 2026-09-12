from api import docker_self_update as dsu
import threading


def _old_info():
    return {
        'Name': '/hermes-webui',
        'Config': {
            'Image': 'repo/webui:old',
            'Env': ['A=1'],
            'Cmd': ['/hermeswebui_init.bash'],
            'Entrypoint': None,
            'User': '1024:1024',
        },
        'HostConfig': {
            'Binds': ['/host/state:/state'],
            'PortBindings': {'8787/tcp': [{'HostPort': '8787'}]},
            'RestartPolicy': {'Name': 'unless-stopped'},
            'NetworkMode': 'project_default',
            'IpcMode': 'private',
            'CgroupnsMode': 'private',
            'Runtime': 'runc',
            'MaskedPaths': ['/proc/kcore'],
            'ReadonlyPaths': ['/proc/sys'],
            'Privileged': False,
            'Links': ['forbidden'],
        },
        'NetworkSettings': {'Networks': {'project_default': {'Aliases': ['hermes-webui']}}},
        'Mounts': [
            {'Type': 'bind', 'Source': '/host/state', 'Destination': '/state', 'RW': True},
            {'Type': 'volume', 'Name': 'agent-data', 'Destination': '/agent', 'RW': False},
        ],
        'State': {'Running': True, 'Health': {'Status': 'healthy'}},
    }


def test_create_payload_preserves_runtime_contract_and_changes_image():
    payload = dsu._create_payload(_old_info(), 'repo/webui:new')
    assert payload['Image'] == 'repo/webui:new'
    assert payload['Env'] == ['A=1']
    assert payload['HostConfig']['Binds'] == ['/host/state:/state']
    assert payload['HostConfig']['PortBindings']['8787/tcp'][0]['HostPort'] == '8787'
    assert payload['HostConfig']['IpcMode'] == 'private'
    assert payload['HostConfig']['CgroupnsMode'] == 'private'
    assert payload['HostConfig']['Runtime'] == 'runc'
    assert payload['HostConfig']['MaskedPaths'] == ['/proc/kcore']
    assert payload['HostConfig']['ReadonlyPaths'] == ['/proc/sys']
    assert 'Links' not in payload['HostConfig']
    assert payload['NetworkingConfig']['EndpointsConfig']['project_default']['Aliases'] == ['hermes-webui']


def test_docker_requests_use_compatible_version_prefix():
    assert dsu.API_PREFIX == '/v1.41'


def test_missing_healthcheck_is_never_accepted():
    class Engine:
        def inspect(self, name): return {'State': {'Running': True}}
    assert dsu._healthy(Engine(), 'webui') is False


def test_runtime_contract_normalizes_empty_defaults():
    assert dsu._normalize_runtime_value(None) is None
    assert dsu._normalize_runtime_value([]) is None
    assert dsu._normalize_runtime_value({}) is None
    assert dsu._normalize_runtime_value(0) is None


def test_runtime_contract_rejects_changed_port_binding():
    old = _old_info()
    new = _old_info()
    new['Id'] = 'new-id'
    new['HostConfig']['PortBindings']['8787/tcp'][0]['HostPort'] = '9999'
    try:
        dsu._verify_runtime_contract(old, new)
    except dsu.DockerEngineError as exc:
        assert 'PortBindings' in str(exc)
    else:
        raise AssertionError('runtime contract mismatch must fail closed')


def test_pull_uses_bounded_long_stream_mode(monkeypatch):
    seen = []
    engine = dsu.DockerEngine()
    monkeypatch.setattr(engine, 'request', lambda *args, **kwargs: seen.append((args, kwargs)))
    engine.pull('repo/webui:latest')
    assert seen[0][1] == {'timeout': 600, 'discard_body': True}


def test_image_release_label_must_match_verified_release():
    class Engine:
        def inspect_image(self, image):
            return {'Config': {'Labels': {'org.opencontainers.image.version': 'v1.2.4'}}}
    try:
        dsu._verified_image_id(Engine(), 'repo/webui:latest', 'v1.2.3')
    except dsu.DockerEngineError:
        pass
    else:
        raise AssertionError('mismatched image/release must fail')


def test_control_request_rejects_arbitrary_actions_and_versions(monkeypatch):
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_SELF_UPDATE', '1')
    busy = threading.Lock()
    for payload in (
        {'action': 'delete', 'channel': 'stable', 'version': 'v1.2.3', 'sha': 'x'},
        {'action': 'update', 'channel': 'stable', 'version': 'latest', 'sha': 'x'},
        {'action': 'update', 'channel': 'other', 'version': 'v1.2.3', 'sha': 'x'},
    ):
        payload['token'] = 'secret'
        response, worker = dsu._control_request(payload, busy=busy, expected_token='secret')
        assert response['ok'] is False
        assert worker is None


def test_control_request_is_single_flight(monkeypatch):
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_SELF_UPDATE', '1')
    busy = threading.Lock()
    assert busy.acquire(blocking=False)
    response, worker = dsu._control_request(
        {'action': 'update', 'channel': 'stable', 'version': 'v1.2.3', 'sha': 'x', 'token': 'secret'}, busy=busy, expected_token='secret'
    )
    assert response['ok'] is False
    assert worker is None
    busy.release()


def test_control_request_rejects_wrong_token(monkeypatch):
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_SELF_UPDATE', '1')
    response, worker = dsu._control_request(
        {'action': 'update', 'channel': 'stable', 'version': 'v1.2.3', 'sha': 'x', 'token': 'wrong'},
        busy=threading.Lock(), expected_token='secret',
    )
    assert response['ok'] is False
    assert worker is None


def test_control_request_rejects_when_not_explicitly_enabled(monkeypatch):
    monkeypatch.delenv('HERMES_WEBUI_DOCKER_SELF_UPDATE', raising=False)
    response, worker = dsu._control_request(
        {'action': 'update', 'channel': 'stable', 'version': 'v1.2.3', 'sha': 'x', 'token': 'secret'},
        busy=threading.Lock(), expected_token='secret',
    )
    assert response == {'ok': False, 'message': 'Docker self-update is disabled'}
    assert worker is None


def test_authenticated_ping_reports_ready_without_starting_update(monkeypatch):
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_SELF_UPDATE', '1')
    response, worker = dsu._control_request(
        {'action': 'ping', 'token': 'secret'},
        busy=threading.Lock(), expected_token='secret',
    )
    assert response == {'ok': True, 'status': 'ready', 'busy': False}
    assert worker is None


def test_ping_rejects_wrong_token(monkeypatch):
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_SELF_UPDATE', '1')
    response, worker = dsu._control_request(
        {'action': 'ping', 'token': 'wrong'},
        busy=threading.Lock(), expected_token='secret',
    )
    assert response == {'ok': False, 'message': 'Invalid update request'}
    assert worker is None


def test_healthcheck_exit_code_tracks_authenticated_ping(monkeypatch):
    monkeypatch.setattr(
        dsu, 'request_health',
        lambda _socket: {'ok': True, 'status': 'ready'},
        raising=False,
    )
    assert dsu.main(['--healthcheck', '/tmp/control.sock']) == 0

    monkeypatch.setattr(
        dsu, 'request_health',
        lambda _socket: {'ok': False, 'message': 'bad'},
        raising=False,
    )
    assert dsu.main(['--healthcheck', '/tmp/control.sock']) == 1


def test_update_request_returns_operation_id_and_authenticated_status(monkeypatch):
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_SELF_UPDATE', '1')
    progress = dsu.UpdateProgress()
    response, worker = dsu._control_request(
        {
            'action': 'update', 'channel': 'stable', 'version': 'v1.2.3',
            'sha': 'abc', 'token': 'secret',
        },
        busy=threading.Lock(), expected_token='secret', progress=progress,
    )
    assert response['ok'] is True
    assert len(response['operation_id']) == 32
    assert response['progress']['stage'] == 'accepted'
    assert response['progress']['step'] == 0
    assert worker is not None

    status, status_worker = dsu._control_request(
        {
            'action': 'status', 'operation_id': response['operation_id'],
            'token': 'secret',
        },
        busy=threading.Lock(), expected_token='secret', progress=progress,
    )
    assert status == {'ok': True, 'progress': response['progress']}
    assert status_worker is None


def test_update_status_rejects_unknown_operation(monkeypatch):
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_SELF_UPDATE', '1')
    response, worker = dsu._control_request(
        {'action': 'status', 'operation_id': '0' * 32, 'token': 'secret'},
        busy=threading.Lock(), expected_token='secret', progress=dsu.UpdateProgress(),
    )
    assert response == {'ok': False, 'message': 'Update operation not found'}
    assert worker is None


def test_progress_keeps_recent_operations_bounded():
    progress = dsu.UpdateProgress()
    operation_ids = []
    for index in range(progress.MAX_OPERATIONS + 2):
        operation_id, _ = progress.start(f'v{index}')
        progress.complete(operation_id)
        operation_ids.append(operation_id)
    assert progress.snapshot(operation_ids[0]) is None
    assert progress.snapshot(operation_ids[1]) is None
    assert progress.snapshot(operation_ids[2])['state'] == 'succeeded'
    assert progress.snapshot(operation_ids[-1])['version'] == f'v{progress.MAX_OPERATIONS + 1}'


def test_update_worker_failure_becomes_terminal_status(monkeypatch):
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_SELF_UPDATE', '1')
    progress = dsu.UpdateProgress()

    def fail_replace(_target, _image, _version, *, progress):
        progress('waiting_for_health', 5)
        progress(
            'rolled_back', 0, state='failed', rolled_back=True,
            failed_stage='waiting_for_health',
        )
        raise dsu.DockerEngineError('hidden implementation detail')

    monkeypatch.setattr(dsu, 'replace_container', fail_replace)
    response, worker = dsu._control_request(
        {
            'action': 'update', 'channel': 'stable', 'version': 'v1.2.3',
            'sha': 'abc', 'token': 'secret',
        },
        busy=threading.Lock(), expected_token='secret', progress=progress,
    )
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    snapshot = progress.snapshot(response['operation_id'])
    assert snapshot['state'] == 'failed'
    assert snapshot['stage'] == 'rolled_back'
    assert snapshot['failed_stage'] == 'waiting_for_health'
    assert snapshot['rolled_back'] is True
    assert 'hidden implementation detail' not in str(snapshot)


def test_pull_failure_reports_old_container_untouched_without_details(monkeypatch):
    progress = dsu.UpdateProgress()
    operation_id, _ = progress.start('v1.2.3')
    progress.advance(operation_id, 'pulling_image', 1)
    progress.fail(operation_id)
    snapshot = progress.snapshot(operation_id)
    assert snapshot['state'] == 'failed'
    assert snapshot['failed_stage'] == 'pulling_image'
    assert snapshot['old_container_untouched'] is True
    assert 'container' not in snapshot
    assert 'image' not in snapshot
    assert 'token' not in snapshot


def test_replace_container_reports_real_stage_order(monkeypatch):
    old = _old_info()

    class Engine:
        def __init__(self):
            self.replacement_started = False

        def inspect(self, target):
            if target == 'hermes-webui' and self.replacement_started:
                result = _old_info()
                result['State'] = {'Running': True, 'Health': {'Status': 'healthy'}}
                return result
            if target == 'hermes-webui':
                return old
            return {'State': {'Running': False}}
        def pull(self, _image): pass
        def inspect_image(self, _image):
            return {'Id': 'sha256:new', 'Config': {'Labels': {'org.opencontainers.image.version': 'v1.2.3'}}}
        def rename(self, _old, _new): pass
        def stop(self, _target): pass
        def create(self, _name, _payload): return {'Id': 'new'}
        def start(self, target):
            if target == 'hermes-webui':
                self.replacement_started = True
        def remove(self, _target, force=False): pass

    engine = Engine()
    monkeypatch.setattr(dsu, 'DockerEngine', lambda: engine)
    monkeypatch.setattr(dsu, '_verify_runtime_contract', lambda _old, _new: None)
    stages = []
    result = dsu.replace_container(
        'hermes-webui', 'repo/webui:latest', 'v1.2.3',
        progress=lambda stage, step, **extra: stages.append((stage, step, extra)),
    )
    assert result['ok'] is True
    assert [item[:2] for item in stages] == [
        ('pulling_image', 1), ('verifying_image', 2),
        ('stopping_old_container', 3), ('starting_new_container', 4),
        ('waiting_for_health', 5), ('verifying_runtime', 6),
        ('cleaning_up', 7),
    ]



def test_replace_container_rolls_back_when_new_container_unhealthy(monkeypatch):
    class Engine:
        def __init__(self): self.calls = []
        def inspect(self, target):
            if target == 'hermes-webui': return _old_info()
            if target == 'hermes-webui.hermes-update-old': return {'State': {'Running': False}}
            return {'State': {'Running': False, 'Health': {'Status': 'unhealthy'}}}
        def pull(self, image): self.calls.append(('pull', image))
        def rename(self, old, new): self.calls.append(('rename', old, new))
        def stop(self, target): self.calls.append(('stop', target))
        def create(self, name, payload): self.calls.append(('create', name)); return {'Id': 'new-id'}
        def start(self, target): self.calls.append(('start', target))
        def remove(self, target, force=False): self.calls.append(('remove', target, force))
    engine = Engine()
    monkeypatch.setattr(dsu, 'DockerEngine', lambda: engine)
    monkeypatch.setattr(dsu.time, 'monotonic', iter([0, 2]).__next__)
    monkeypatch.setattr(dsu.time, 'sleep', lambda _: None)
    try:
        dsu.replace_container('hermes-webui', 'repo/webui:new', timeout=1)
    except dsu.DockerEngineError:
        pass
    else:
        raise AssertionError('unhealthy replacement must fail')
    assert ('rename', 'hermes-webui.hermes-update-old', 'hermes-webui') in engine.calls
    assert ('start', 'hermes-webui') in engine.calls


def test_replace_container_rejects_stopped_target(monkeypatch):
    old = _old_info()
    old['State'] = {'Running': False}

    class FakeEngine:
        def inspect(self, _name):
            return old

    monkeypatch.setattr(dsu, 'DockerEngine', FakeEngine)
    try:
        dsu.replace_container('hermes-webui', 'repo/webui:new', 'v1.0.0')
    except dsu.DockerEngineError as exc:
        assert str(exc) == 'target container is not running'
    else:
        raise AssertionError('stopped target must fail closed')
