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
