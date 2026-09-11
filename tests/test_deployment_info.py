from pathlib import Path

from api import updates


def test_deployment_info_respects_explicit_binary_override(monkeypatch):
    monkeypatch.setenv('HERMES_WEBUI_DEPLOYMENT_TYPE', 'binary')
    monkeypatch.setattr(Path, 'exists', lambda self: str(self) == str(updates.REPO_ROOT / '.git'))
    result = updates.deployment_info()
    assert result['type'] == 'binary'
    assert result['online_update'] is True
    assert result['update_mode'] == 'git'


def test_packaged_binary_without_git_is_display_only(monkeypatch):
    monkeypatch.setenv('HERMES_WEBUI_DEPLOYMENT_TYPE', 'binary')
    monkeypatch.setattr(Path, 'exists', lambda self: False)
    result = updates.deployment_info()
    assert result['online_update'] is False
    assert result['update_mode'] == 'manual'


def test_deployment_info_docker_requires_sidecar_control_socket(monkeypatch):
    monkeypatch.setenv('HERMES_WEBUI_DEPLOYMENT_TYPE', 'docker')
    monkeypatch.delenv('HERMES_WEBUI_DOCKER_SELF_UPDATE', raising=False)
    assert updates.deployment_info()['online_update'] is False
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_SELF_UPDATE', '1')
    monkeypatch.setattr(Path, 'exists', lambda self: str(self) == updates.CONTROL_SOCKET)
    monkeypatch.setattr(updates.os, 'access', lambda *args: True)
    result = updates.deployment_info()
    assert result['type'] == 'docker'
    assert result['online_update'] is True
    assert result['update_mode'] == 'docker_engine'


def test_docker_update_rejects_without_sidecar(monkeypatch):
    monkeypatch.setenv('HERMES_WEBUI_DEPLOYMENT_TYPE', 'docker')
    monkeypatch.delenv('HERMES_WEBUI_DOCKER_SELF_UPDATE', raising=False)
    result = updates.apply_docker_update()
    assert result['ok'] is False
    assert 'sidecar' in result['message']


def test_docker_update_requests_sidecar_with_verified_release(monkeypatch):
    monkeypatch.setenv('HERMES_WEBUI_DEPLOYMENT_TYPE', 'docker')
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_SELF_UPDATE', '1')
    monkeypatch.setattr(Path, 'exists', lambda self: str(self) == updates.CONTROL_SOCKET)
    monkeypatch.setattr(updates.os, 'access', lambda *args: True)
    monkeypatch.setattr(updates, 'check_for_updates', lambda **kwargs: {
        'webui': {'latest_version': 'v0.53.0', 'latest_sha': 'abc123'},
    })
    seen = []
    monkeypatch.setattr(updates, 'request_update', lambda *args: seen.append(args) or {
        'ok': True, 'restart_scheduled': True,
    })
    result = updates.apply_docker_update('stable')
    assert result['ok'] is True
    assert seen == [('stable', 'v0.53.0', 'abc123')]
