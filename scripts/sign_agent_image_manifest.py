"""Offline signer: requires an external Ed25519 private key and test receipt.

This tool does not build, push, deploy, or create a key. Release engineering
must independently validate all supplied identities and the receipt first.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from api.agent_image_manifest import ManifestError, verify_manifest


def sign_release_artifact(*, manifest: dict, receipt: bytes, private_key_pem: bytes,
                          public_key: bytes) -> tuple[bytes, str]:
    """Sign exact canonical bytes after checking a bound test receipt.

    A receipt is a declaration from a separate compatibility runner, not
    proof of execution. The release operator must authenticate its producer.
    """
    if not receipt or len(receipt) > 1024 * 1024:
        raise ManifestError('Compatibility receipt is empty or too large')
    try:
        evidence = json.loads(receipt)
    except (ValueError, UnicodeError) as exc:
        raise ManifestError('Compatibility receipt is not JSON') from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get('verification'), dict):
        raise ManifestError('Invalid release candidate')
    if not isinstance(evidence, dict) or evidence.get('passed') is not True or evidence != {
        'suite': 'agent-image-compatibility', 'passed': True,
        'repository': manifest.get('repository'), 'digest': manifest.get('digest'),
        'platform': manifest.get('platform'), 'webui_commit': manifest.get('webui_commit'),
        'agent_commit': manifest.get('agent_commit'),
    }:
        raise ManifestError('Compatibility receipt does not bind the image and commits')
    if manifest.get('verification', {}).get('receipt_sha256') != hashlib.sha256(receipt).hexdigest():
        raise ManifestError('Compatibility receipt hash mismatch')
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ManifestError('Signing key must be Ed25519')
    raw = json.dumps(manifest, sort_keys=True, separators=(',', ':'),
                     ensure_ascii=True, allow_nan=False).encode('ascii')
    signature = base64.b64encode(key.sign(raw)).decode('ascii')
    verify_manifest(raw, signature, public_key, repository=manifest['repository'],
                    platform=manifest['platform'], agent_commit=manifest['agent_commit'])
    return raw, signature


def main() -> None:
    parser = argparse.ArgumentParser(description='Sign one verified WebUI+Agent image artifact (offline)')
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--private-key', type=Path, required=True)
    parser.add_argument('--public-key', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    raw, signature = sign_release_artifact(
        manifest=manifest, receipt=args.receipt.read_bytes(),
        private_key_pem=args.private_key.read_bytes(),
        public_key=base64.b64decode(args.public_key.read_text(encoding='ascii').strip(), validate=True),
    )
    # Refuse to overwrite an existing artifact. Publish both files together.
    signature_path = Path(str(args.output) + '.sig')
    if args.output.exists() or signature_path.exists():
        raise FileExistsError('Output artifact already exists')
    with os.fdopen(os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as out:
        out.write(raw)
    try:
        with os.fdopen(os.open(signature_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as out:
            out.write(signature.encode('ascii'))
    except BaseException:
        args.output.unlink()
        raise
    print('Signed manifest and detached signature written')


if __name__ == '__main__':
    main()
