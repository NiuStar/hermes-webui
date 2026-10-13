"""An operator-pinned immutable image needs no detached publisher signature."""
import json
import threading

import pytest

from api import docker_self_update as dsu


REPOSITORY = '24802117/hermes-webui'
AGENT = 'c' * 40
WEBUI = 'b' * 40
DIGEST = 'sha256:' + 'a' * 64


def release(tmp_path):
    path = tmp_path / 'release.json'
    path.write_text(json.dumps({
        'repository': REPOSITORY, 'digest': DIGEST, 'platform': 'linux/amd64',
        'webui_commit': WEBUI, 'webui_version': 'v2026.10.13-r1',
        'agent_commit': AGENT, 'agent_version': 'v0.21.7',
    }))
    path.chmod(0o600)
    return path


def test_unsigned_operator_release_is_pinned_without_publisher_key(tmp_path, monkeypatch):
    path = release(tmp_path)
    for key in ('HERMES_WEBUI_AGENT_MANIFEST_PUBKEY',
                'HERMES_WEBUI_AGENT_MANIFEST_SIGNATURE_PATH',
                'HERMES_WEBUI_AGENT_MANIFEST_PUBKEY_SHA256'):
        monkeypatch.delenv(key, raising=False)
    expected = dsu.preflight_agent_release(path, repository=REPOSITORY,
                                            platform='linux/amd64', agent_commit=AGENT)
    assert expected['image'] == REPOSITORY + '@' + DIGEST
    assert expected['webui_commit'] == WEBUI


@pytest.mark.parametrize('change', [
    {'digest': 'latest'}, {'agent_commit': 'f' * 40},
    {'platform': 'linux/arm64'}, {'repository': 'another/repository'},
    {'extra': True},
])
def test_unsigned_release_rejects_invalid_target(tmp_path, change):
    path = release(tmp_path)
    data = json.loads(path.read_text()); data.update(change)
    path.write_text(json.dumps(data))
    with pytest.raises(dsu.DockerEngineError):
        dsu.preflight_agent_release(path, repository=REPOSITORY,
                                    platform='linux/amd64', agent_commit=AGENT)


def test_unsigned_release_rejects_unsafe_file(tmp_path):
    path = release(tmp_path); path.chmod(0o666)
    with pytest.raises(dsu.DockerEngineError):
        dsu.preflight_agent_release(path, repository=REPOSITORY, platform='linux/amd64')
    path.chmod(0o600)
    link = tmp_path / 'link.json'; link.symlink_to(path)
    with pytest.raises(dsu.DockerEngineError):
        dsu.preflight_agent_release(link, repository=REPOSITORY, platform='linux/amd64')


def test_unsigned_release_cannot_be_selected_by_browser_payload(tmp_path, monkeypatch):
    path = release(tmp_path)
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_SELF_UPDATE', '1')
    monkeypatch.setenv('HERMES_WEBUI_AGENT_RELEASE_FILE', str(path))
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_IMAGE', REPOSITORY)
    monkeypatch.setenv('HERMES_WEBUI_AGENT_COMMIT', AGENT)
    monkeypatch.delenv('HERMES_WEBUI_AGENT_MANIFEST_PATH', raising=False)
    monkeypatch.delenv('HERMES_WEBUI_AGENT_MANIFEST_SIGNATURE_PATH', raising=False)
    class Engine:
        def inspect(self, *_):
            raise dsu.DockerEngineError('no runtime target')
        def pull(self, *_):
            pytest.fail('preflight must not pull')
    monkeypatch.setattr(dsu, 'DockerEngine', Engine)
    result, worker = dsu._control_request({
        'action': 'agent_preflight', 'token': 'secret',
        'image': 'attacker.example/bad:latest', 'release_file': '/tmp/other.json',
    }, busy=threading.Lock(), expected_token='secret')
    assert not result['ok'] and worker is None


def test_unsigned_release_preflight_reaches_unmanaged_runtime_without_signature(tmp_path, monkeypatch):
    path = release(tmp_path)
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_SELF_UPDATE', '1')
    monkeypatch.setenv('HERMES_WEBUI_AGENT_RELEASE_FILE', str(path))
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_IMAGE', REPOSITORY)
    monkeypatch.delenv('HERMES_WEBUI_AGENT_MANIFEST_PATH', raising=False)
    monkeypatch.delenv('HERMES_WEBUI_AGENT_MANIFEST_SIGNATURE_PATH', raising=False)
    monkeypatch.delenv('HERMES_WEBUI_AGENT_COMMIT', raising=False)
    class Engine:
        def inspect(self, *_):
            return {'Image':'sha256:'+'f'*64, 'State':{'Running': True},
                'Config':{'Env':['HERMES_WEBUI_AGENT_DIR=/opt/hermes'],'Labels':{}},
                'HostConfig':{'Binds':[]}, 'Mounts':[]}
        def inspect_image(self, image):
            assert image == 'sha256:'+'f'*64
            return {'Id':image, 'Config':{'Labels':{
                'org.opencontainers.image.version':'v2026.10.13-r1',
                'org.opencontainers.image.revision':WEBUI,
                'org.opencontainers.image.hermes-agent.path':'/opt/hermes',
                'org.opencontainers.image.hermes-agent.revision':'d'*40}}}
        def pull(self, *_): pytest.fail('preflight must not pull')
    monkeypatch.setattr(dsu, 'DockerEngine', Engine)
    result, worker = dsu._control_request({'action':'agent_preflight','token':'secret'},
        busy=threading.Lock(),expected_token='secret')
    assert result['ok'] is True and worker is None
    assert result['latest_sha'] == AGENT


def test_unsigned_release_and_signed_config_mix_fails_before_inspect(tmp_path, monkeypatch):
    path = release(tmp_path)
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_SELF_UPDATE', '1')
    monkeypatch.setenv('HERMES_WEBUI_AGENT_RELEASE_FILE', str(path))
    monkeypatch.setenv('HERMES_WEBUI_AGENT_MANIFEST_PATH', str(path))
    monkeypatch.setenv('HERMES_WEBUI_AGENT_MANIFEST_SIGNATURE_PATH', str(path))
    class Engine:
        def inspect(self, *_):pytest.fail('mixed config must not inspect')
    monkeypatch.setattr(dsu, 'DockerEngine', Engine)
    result, worker = dsu._control_request({'action':'agent_preflight','token':'secret'},
        busy=threading.Lock(),expected_token='secret')
    assert result['ok'] is False and worker is None
