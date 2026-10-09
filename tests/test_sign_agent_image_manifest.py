import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from api.agent_image_manifest import ManifestError, verify_manifest
from scripts.sign_agent_image_manifest import sign_release_artifact


def test_offline_signer_produces_verifiable_manifest():
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    manifest = {
        'schema': 'hermes-webui-agent-image/v1', 'repository': '24802117/hermes-webui',
        'digest': 'sha256:' + 'a' * 64, 'platform': 'linux/amd64',
        'webui_commit': 'b' * 40, 'agent_commit': 'c' * 40,
        'webui_version': 'v2026.10.08-r2', 'agent_version': 'v0.21.6',
        'verification': {'suite': 'agent-image-compatibility', 'passed': True,
                         'receipt_sha256': ''},
    }
    receipt = json.dumps({
        'suite': 'agent-image-compatibility', 'passed': True,
        'repository': manifest['repository'], 'digest': manifest['digest'],
        'platform': manifest['platform'], 'webui_commit': manifest['webui_commit'],
        'agent_commit': manifest['agent_commit'],
    }).encode()
    manifest['verification']['receipt_sha256'] = hashlib.sha256(receipt).hexdigest()
    raw, signature = sign_release_artifact(manifest=manifest, receipt=receipt,
                                            private_key_pem=pem, public_key=public)
    assert json.loads(raw) == manifest
    assert len(base64.b64decode(signature)) == 64
    assert verify_manifest(raw, signature, public, repository=manifest['repository'],
                           platform=manifest['platform'], agent_commit=manifest['agent_commit']) == manifest
    with pytest.raises(ManifestError):
        sign_release_artifact(manifest=manifest, receipt=b'failed', private_key_pem=pem,
                              public_key=public)
    forged = receipt.replace(b'true', b'1')
    manifest['verification']['receipt_sha256'] = hashlib.sha256(forged).hexdigest()
    with pytest.raises(ManifestError):
        sign_release_artifact(manifest=manifest, receipt=forged, private_key_pem=pem,
                              public_key=public)
