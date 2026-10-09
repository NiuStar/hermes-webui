"""Agent manifest preflight must be sidecar-owned and perform no Docker writes."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from api import docker_self_update as dsu


def _artifact(tmp_path):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    manifest = {
        'schema': 'hermes-webui-agent-image/v1',
        'repository': '24802117/hermes-webui', 'digest': 'sha256:' + 'a' * 64,
        'platform': 'linux/amd64', 'webui_commit': 'b' * 40,
        'agent_commit': 'c' * 40, 'webui_version': 'v2026.10.08-r2',
        'agent_version': 'v0.21.6',
        'verification': {'suite': 'agent-image-compatibility', 'passed': True,
                         'receipt_sha256': 'd' * 64},
    }
    raw = json.dumps(manifest, sort_keys=True).encode()
    (tmp_path / 'manifest.json').write_bytes(raw)
    (tmp_path / 'manifest.json.sig').write_text(base64.b64encode(key.sign(raw)).decode())
    (tmp_path / 'trusted.pub').write_text(base64.b64encode(public).decode())
    return manifest


def test_sidecar_preflight_only_returns_verified_digest(tmp_path, monkeypatch):
    manifest = _artifact(tmp_path)
    monkeypatch.setenv('HERMES_WEBUI_AGENT_MANIFEST_PUBKEY', str(tmp_path / 'trusted.pub'))
    monkeypatch.setenv('HERMES_WEBUI_AGENT_MANIFEST_PUBKEY_SHA256',
                       hashlib.sha256(base64.b64decode((tmp_path / 'trusted.pub').read_text())).hexdigest())
    result = dsu.preflight_agent_manifest(
        tmp_path / 'manifest.json', tmp_path / 'manifest.json.sig',
        repository=manifest['repository'], platform=manifest['platform'],
        agent_commit=manifest['agent_commit'],
    )
    assert result['image'] == '24802117/hermes-webui@sha256:' + 'a' * 64
    assert result['webui_commit'] == 'b' * 40


@pytest.mark.parametrize('failure', ['no-key', 'wrong-key', 'tamper', 'bad-mode', 'symlink', 'fifo'])
def test_sidecar_preflight_fail_closed(tmp_path, monkeypatch, failure):
    manifest = _artifact(tmp_path)
    pub = tmp_path / 'trusted.pub'
    monkeypatch.setenv('HERMES_WEBUI_AGENT_MANIFEST_PUBKEY', str(pub))
    monkeypatch.setenv('HERMES_WEBUI_AGENT_MANIFEST_PUBKEY_SHA256',
                       hashlib.sha256(base64.b64decode(pub.read_text())).hexdigest())
    if failure == 'no-key':
        monkeypatch.delenv('HERMES_WEBUI_AGENT_MANIFEST_PUBKEY')
    elif failure == 'wrong-key':
        pub.write_text(base64.b64encode(os.urandom(32)).decode())
    elif failure == 'tamper':
        (tmp_path / 'manifest.json').write_text('{}')
    elif failure == 'bad-mode':
        pub.chmod(0o666)
    elif failure == 'symlink':
        actual = tmp_path / 'actual.pub'
        pub.rename(actual)
        pub.symlink_to(actual)
    elif failure == 'fifo':
        pub.unlink()
        os.mkfifo(pub)
    with pytest.raises(dsu.DockerEngineError):
        dsu.preflight_agent_manifest(
            tmp_path / 'manifest.json', tmp_path / 'manifest.json.sig',
            repository=manifest['repository'], platform=manifest['platform'],
            agent_commit=manifest['agent_commit'],
        )


def test_signed_agent_payload_cannot_start_sidecar_update(monkeypatch):
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_SELF_UPDATE', '1')
    for action in ('update_agent', 'preflight_agent'):
        response, worker = dsu._control_request({
            'action': action, 'channel': 'stable', 'version': 'v2026.10.08-r2',
            'sha': 'c' * 40, 'token': 'secret',
            'manifest': '{"passed":true}',
            'image': '24802117/hermes-webui@sha256:' + 'a' * 64,
        }, busy=threading.Lock(), expected_token='secret')
        assert response['ok'] is False
        assert worker is None


@pytest.mark.parametrize('mount_target', ['/app', '/app/venv', '/app/hermes-agent-src-alt'])
def test_readonly_preflight_rejects_persistent_app_before_offering_update(tmp_path, monkeypatch, mount_target):
    manifest = _artifact(tmp_path)
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_SELF_UPDATE', '1')
    monkeypatch.setenv('HERMES_WEBUI_AGENT_MANIFEST_PATH', str(tmp_path / 'manifest.json'))
    monkeypatch.setenv('HERMES_WEBUI_AGENT_MANIFEST_SIGNATURE_PATH', str(tmp_path / 'manifest.json.sig'))
    monkeypatch.setenv('HERMES_WEBUI_AGENT_MANIFEST_PUBKEY', str(tmp_path / 'trusted.pub'))
    monkeypatch.setenv('HERMES_WEBUI_AGENT_MANIFEST_PUBKEY_SHA256',
                       hashlib.sha256(base64.b64decode((tmp_path / 'trusted.pub').read_text())).hexdigest())
    monkeypatch.setenv('HERMES_WEBUI_AGENT_COMMIT', manifest['agent_commit'])
    monkeypatch.setenv('HERMES_WEBUI_UPDATE_TARGET', 'hermes-webui')
    class Engine:
        def inspect(self, name):
            assert name == 'hermes-webui'
            return {'State': {'Running': True},
                    'Config': {'Env': ['HERMES_WEBUI_AGENT_DIR=/opt/hermes']},
                    'HostConfig': {'Binds': ['/state:' + mount_target + ':rw']},
                    'Mounts': [{'Destination': mount_target, 'Type': 'bind'}]}
        def pull(self, *_): raise AssertionError('read-only preflight pulled an image')
    monkeypatch.setattr(dsu, 'DockerEngine', Engine)
    result, worker = dsu._control_request({'action': 'agent_preflight', 'token': 'secret'},
                                           busy=threading.Lock(), expected_token='secret')
    assert result['ok'] is False and worker is None


def test_readonly_preflight_rejects_same_baked_agent_before_any_pull(tmp_path, monkeypatch):
    manifest = _artifact(tmp_path)
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_SELF_UPDATE', '1')
    monkeypatch.setenv('HERMES_WEBUI_AGENT_MANIFEST_PATH', str(tmp_path / 'manifest.json'))
    monkeypatch.setenv('HERMES_WEBUI_AGENT_MANIFEST_SIGNATURE_PATH', str(tmp_path / 'manifest.json.sig'))
    monkeypatch.setenv('HERMES_WEBUI_AGENT_MANIFEST_PUBKEY', str(tmp_path / 'trusted.pub'))
    monkeypatch.setenv('HERMES_WEBUI_AGENT_MANIFEST_PUBKEY_SHA256',
                       hashlib.sha256(base64.b64decode((tmp_path / 'trusted.pub').read_text())).hexdigest())
    monkeypatch.setenv('HERMES_WEBUI_AGENT_COMMIT', manifest['agent_commit'])
    class Engine:
        def inspect(self, name):
            assert name == 'hermes-webui'
            return {'Image': 'sha256:' + 'f' * 64, 'State': {'Running': True},
                    'Config': {'Env': ['HERMES_WEBUI_AGENT_DIR=/opt/hermes']},
                    'HostConfig': {'Binds': []}, 'Mounts': []}
        def inspect_image(self, image):
            assert image == 'sha256:' + 'f' * 64
            return {'Id': image, 'Config': {'Labels': {
                'org.opencontainers.image.hermes-agent.path': '/opt/hermes',
                'org.opencontainers.image.hermes-agent.revision': manifest['agent_commit']}}}
        def pull(self, *_): pytest.fail('same Agent commit must not pull')
    monkeypatch.setattr(dsu, 'DockerEngine', Engine)
    for action in ('agent_preflight', 'update_agent'):
        result, worker = dsu._control_request({'action': action, 'token': 'secret'},
                                               busy=threading.Lock(), expected_token='secret')
        assert result['ok'] is False and worker is None


def test_compose_owned_target_cannot_be_replaced_without_reconciled_pin(tmp_path, monkeypatch):
    manifest = _artifact(tmp_path)
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_SELF_UPDATE', '1')
    for key, value in {
        'HERMES_WEBUI_AGENT_MANIFEST_PATH': str(tmp_path / 'manifest.json'),
        'HERMES_WEBUI_AGENT_MANIFEST_SIGNATURE_PATH': str(tmp_path / 'manifest.json.sig'),
        'HERMES_WEBUI_AGENT_MANIFEST_PUBKEY': str(tmp_path / 'trusted.pub'),
        'HERMES_WEBUI_AGENT_MANIFEST_PUBKEY_SHA256': hashlib.sha256(
            base64.b64decode((tmp_path / 'trusted.pub').read_text())).hexdigest(),
        'HERMES_WEBUI_AGENT_COMMIT': manifest['agent_commit'],
        'HERMES_WEBUI_UPDATE_TARGET': 'hermes-webui',
    }.items():
        monkeypatch.setenv(key, value)

    class Engine:
        def inspect(self, name):
            assert name == 'hermes-webui'
            return {'Image': 'sha256:' + 'f' * 64, 'State': {'Running': True},
                    'Config': {'Env': ['HERMES_WEBUI_AGENT_DIR=/opt/hermes'],
                               'Labels': {'com.docker.compose.project': 'prod',
                                          'com.docker.compose.service': 'hermes-webui'}},
                    'HostConfig': {'Binds': []}, 'Mounts': []}
        def inspect_image(self, image):
            assert image == 'sha256:' + 'f' * 64
            return {'Id': image, 'Config': {'Labels': {
                'org.opencontainers.image.hermes-agent.path': '/opt/hermes',
                'org.opencontainers.image.hermes-agent.revision': 'e' * 40}}}
        def pull(self, *_): pytest.fail('Compose-owned target must not pull')

    monkeypatch.setattr(dsu, 'DockerEngine', Engine)
    for action in ('agent_preflight', 'update_agent'):
        response, worker = dsu._control_request({'action': action, 'token': 'secret'},
                                                busy=threading.Lock(), expected_token='secret')
        assert response['ok'] is False and worker is None
        assert response['message'] == 'Signed Agent release cannot be authenticated'


def test_sidecar_direct_script_mode_imports_manifest_verifier(tmp_path):
    manifest = _artifact(tmp_path)
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'HERMES_WEBUI_AGENT_MANIFEST_PUBKEY': str(tmp_path / 'trusted.pub')}
    env['HERMES_WEBUI_AGENT_MANIFEST_PUBKEY_SHA256'] = hashlib.sha256(
        base64.b64decode((tmp_path / 'trusted.pub').read_text())).hexdigest()
    env.pop('PYTHONPATH', None)
    code = ("import importlib.util, pathlib, sys; "
            "path=pathlib.Path(sys.argv[1]); "
            "sys.path.insert(0, str(path.parent)); "
            "spec=importlib.util.spec_from_file_location('docker_self_update', path); "
            "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
            "result=module.preflight_agent_manifest(sys.argv[2],sys.argv[3],"
            "repository=sys.argv[4],platform=sys.argv[5],agent_commit=sys.argv[6]); "
            "print(result['image'])")
    result = subprocess.run([
        sys.executable, '-c', code, str(root / 'api/docker_self_update.py'),
        str(tmp_path / 'manifest.json'), str(tmp_path / 'manifest.json.sig'),
        manifest['repository'], manifest['platform'], manifest['agent_commit'],
    ], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == manifest['repository'] + '@' + manifest['digest']


@pytest.mark.parametrize('fingerprint', ['', '0' * 64, 'not-a-sha'])
def test_sidecar_rejects_absent_or_mismatched_operator_key_pin(tmp_path, monkeypatch, fingerprint):
    manifest = _artifact(tmp_path)
    monkeypatch.setenv('HERMES_WEBUI_AGENT_MANIFEST_PUBKEY', str(tmp_path / 'trusted.pub'))
    monkeypatch.setenv('HERMES_WEBUI_AGENT_MANIFEST_PUBKEY_SHA256', fingerprint)
    with pytest.raises(dsu.DockerEngineError):
        dsu.preflight_agent_manifest(
            tmp_path / 'manifest.json', tmp_path / 'manifest.json.sig',
            repository=manifest['repository'], platform=manifest['platform'],
            agent_commit=manifest['agent_commit'])
