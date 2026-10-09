"""Fail-closed setup validation for the optional single-container Agent updater."""
import base64
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.check_agent_update_setup import SetupError, verify_setup


def _settings(tmp_path):
    artifact = tmp_path / 'release'
    artifact.mkdir(mode=0o700, parents=True)
    pub = artifact / 'publisher.pub'
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    pub.write_text(base64.b64encode(public).decode())
    manifest = artifact / 'manifest.json'
    manifest.write_text(json.dumps({
        'schema': 'hermes-webui-agent-image/v1',
        'repository': '24802117/hermes-webui',
        'digest': 'sha256:' + 'c' * 64,
        'platform': 'linux/amd64',
        'webui_commit': 'd' * 40,
        'agent_commit': 'b' * 40,
        'webui_version': 'v2026.10.08-test',
        'agent_version': 'v0.21.6',
        'verification': {'suite': 'agent-image-compatibility', 'passed': True,
                         'receipt_sha256': 'e' * 64},
    }))
    signature = artifact / 'manifest.json.sig'
    signature.write_text(base64.b64encode(key.sign(manifest.read_bytes())).decode())
    return {
        'HERMES_WEBUI_ACCEPTED_IMAGE': '24802117/hermes-webui@sha256:' + 'a' * 64,
        'HERMES_WEBUI_DOCKER_IMAGE': '24802117/hermes-webui',
        'HERMES_WEBUI_AGENT_COMMIT': 'b' * 40,
        'HERMES_WEBUI_AGENT_MANIFEST_FILE': str(manifest),
        'HERMES_WEBUI_AGENT_MANIFEST_SIGNATURE_FILE': str(signature),
        'HERMES_WEBUI_AGENT_PUBLISHER_PUBKEY_FILE': str(pub),
        'HERMES_WEBUI_AGENT_MANIFEST_PUBKEY_SHA256': hashlib.sha256(public).hexdigest(),
    }


def test_valid_immutable_contract_has_no_side_effects(tmp_path):
    env = _settings(tmp_path)
    # The accepted image is the *current* Compose pin; the signed manifest
    # names the *next* update. They must not be forced to the same digest.
    signed = json.loads(Path(env['HERMES_WEBUI_AGENT_MANIFEST_FILE']).read_text())
    assert env['HERMES_WEBUI_ACCEPTED_IMAGE'] != signed['repository'] + '@' + signed['digest']
    assert verify_setup(env)['image'] == env['HERMES_WEBUI_ACCEPTED_IMAGE']
    assert sorted(p.name for p in (tmp_path / 'release').iterdir()) == [
        'manifest.json', 'manifest.json.sig', 'publisher.pub']


@pytest.mark.parametrize('mutation', [
    {'HERMES_WEBUI_ACCEPTED_IMAGE': '24802117/hermes-webui:latest'},
    {'HERMES_WEBUI_ACCEPTED_IMAGE': 'wrong/repo@sha256:' + 'a' * 64},
    {'HERMES_WEBUI_AGENT_COMMIT': 'b' * 12},
    {'HERMES_WEBUI_AGENT_MANIFEST_PUBKEY_SHA256': '0' * 64},
])
def test_rejects_unpinned_or_mismatched_identity(tmp_path, mutation):
    env = _settings(tmp_path)
    env.update(mutation)
    with pytest.raises(SetupError):
        verify_setup(env)


def test_rejects_missing_or_symlinked_release_material(tmp_path):
    env = _settings(tmp_path)
    Path(env['HERMES_WEBUI_AGENT_MANIFEST_FILE']).unlink()
    with pytest.raises(SetupError):
        verify_setup(env)
    env = _settings(tmp_path / 'second')
    pub = Path(env['HERMES_WEBUI_AGENT_PUBLISHER_PUBKEY_FILE'])
    copied = pub.with_name('actual.pub')
    pub.rename(copied)
    pub.symlink_to(copied)
    with pytest.raises(SetupError):
        verify_setup(env)


def test_rejects_signed_release_for_different_commit(tmp_path):
    env = _settings(tmp_path)
    env['HERMES_WEBUI_AGENT_COMMIT'] = 'a' * 40
    with pytest.raises(SetupError):
        verify_setup(env)


def test_rejects_modified_manifest_after_signing(tmp_path):
    env = _settings(tmp_path)
    manifest = Path(env['HERMES_WEBUI_AGENT_MANIFEST_FILE'])
    manifest.write_bytes(manifest.read_bytes() + b' ')
    with pytest.raises(SetupError):
        verify_setup(env)


def test_rejects_unknown_host_architecture(tmp_path, monkeypatch):
    env = _settings(tmp_path)
    monkeypatch.setattr('scripts.check_agent_update_setup.os.uname',
                        lambda: type('Host', (), {'machine': 'riscv64'})())
    with pytest.raises(SetupError, match='platform'):
        verify_setup(env)
