"""Signed image manifests are data, never an authority supplied by the browser."""
import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from api.agent_image_manifest import ManifestError, verify_manifest


@pytest.fixture
def signed_manifest():
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    manifest = {
        'schema': 'hermes-webui-agent-image/v1',
        'repository': '24802117/hermes-webui',
        'digest': 'sha256:' + 'a' * 64,
        'platform': 'linux/amd64',
        'webui_commit': 'b' * 40,
        'agent_commit': 'c' * 40,
        'webui_version': 'v2026.10.08-r2',
        'agent_version': 'v0.21.6',
        'verification': {'suite': 'agent-image-compatibility', 'passed': True,
                         'receipt_sha256': 'd' * 64},
    }
    raw = json.dumps(manifest, separators=(',', ':'), sort_keys=True).encode()
    return key, public, manifest, raw, base64.b64encode(key.sign(raw)).decode()


def test_verified_manifest_binds_exact_artifact(signed_manifest):
    _, public, manifest, raw, signature = signed_manifest
    result = verify_manifest(raw, signature, public, repository='24802117/hermes-webui',
                             platform='linux/amd64', agent_commit='c' * 40)
    assert result == manifest
    assert result['repository'] + '@' + result['digest'] == '24802117/hermes-webui@sha256:' + 'a' * 64


@pytest.mark.parametrize('field,value', [
    ('platform', 'linux/arm64'), ('repository', 'other/repo'),
    ('agent_commit', 'e' * 40), ('digest', 'sha256:invalid'),
    ('webui_commit', 'short'), ('schema', 'untrusted/v1'),
    ('verification', {'suite': 'agent-image-compatibility', 'passed': False,
                      'receipt_sha256': 'd' * 64}),
    ('platform', ['linux/amd64']),
])
def test_mismatch_or_tamper_is_rejected(signed_manifest, field, value):
    _, public, _, raw, signature = signed_manifest
    tampered = json.loads(raw)
    tampered[field] = value
    with pytest.raises(ManifestError):
        verify_manifest(json.dumps(tampered).encode(), signature, public,
                        repository='24802117/hermes-webui', platform='linux/amd64', agent_commit='c' * 40)


def test_wrong_signer_rejected(signed_manifest):
    _, _, _, raw, signature = signed_manifest
    other = Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    with pytest.raises(ManifestError):
        verify_manifest(raw, signature, other, repository='24802117/hermes-webui',
                        platform='linux/amd64', agent_commit='c' * 40)


def test_loopback_registry_with_port_for_isolated_qa(signed_manifest):
    key, public, manifest, _, _ = signed_manifest
    manifest['repository'] = 'localhost:18973/hermes-webui-agent-qa'
    raw = json.dumps(manifest).encode()
    assert verify_manifest(raw, base64.b64encode(key.sign(raw)).decode(), public,
                           repository=manifest['repository'], platform='linux/amd64',
                           agent_commit='c' * 40) == manifest


@pytest.mark.parametrize('repository', ['localhost:18973/../bad', 'localHOST:18973/repo',
                                       'localhost:abc/repo', 'https://localhost:18973/repo'])
def test_invalid_registry_repository_rejected(signed_manifest, repository):
    key, public, manifest, _, _ = signed_manifest
    manifest['repository'] = repository
    raw = json.dumps(manifest).encode()
    with pytest.raises(ManifestError):
        verify_manifest(raw, base64.b64encode(key.sign(raw)).decode(), public,
                        repository=repository, platform='linux/amd64', agent_commit='c' * 40)


def test_validly_signed_malformed_manifest_rejected(signed_manifest):
    key, public, _, _, _ = signed_manifest
    payloads = [
        b'{"schema":"hermes-webui-agent-image/v1","schema":"hermes-webui-agent-image/v1"}',
        b'[]', b'{' + b' ' * 16384 + b'}', b'{"schema": NaN}',
    ]
    for raw in payloads:
        with pytest.raises(ManifestError):
            verify_manifest(raw, base64.b64encode(key.sign(raw)).decode(), public,
                            repository='24802117/hermes-webui', platform='linux/amd64', agent_commit='c' * 40)


def test_unsigned_extra_fields_are_forbidden_even_when_signed(signed_manifest):
    key, public, manifest, _, _ = signed_manifest
    manifest['image_url'] = 'https://attacker.invalid/image'
    raw = json.dumps(manifest).encode()
    with pytest.raises(ManifestError):
        verify_manifest(raw, base64.b64encode(key.sign(raw)).decode(), public,
                        repository='24802117/hermes-webui', platform='linux/amd64', agent_commit='c' * 40)
