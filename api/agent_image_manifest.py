"""Strict, independently signed identity for a Docker WebUI+Agent artifact.

Verification does not authorize an update by itself. The updater must use a
trusted, separately provisioned public key and pull the returned digest only.
"""
from __future__ import annotations

import base64
import binascii
import json
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class ManifestError(ValueError):
    """Untrusted or incomplete image provenance."""


_KEYS = frozenset({
    'schema', 'repository', 'digest', 'platform', 'webui_commit',
    'agent_commit', 'webui_version', 'agent_version', 'verification',
})
_VERIFICATION_KEYS = frozenset({'suite', 'passed', 'receipt_sha256'})
_SHA = re.compile(r'[0-9a-f]{40}\Z')
_DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
_REPO = re.compile(r'(?:[a-z0-9][a-z0-9._-]*(?::[0-9]{1,5})?/)(?:[a-z0-9][a-z0-9._-]*/)*[a-z0-9][a-z0-9._-]*\Z')
_VERSION = re.compile(r'v[0-9][0-9A-Za-z_.-]*\Z')


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ManifestError('Duplicate manifest field')
        result[name] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ManifestError('Non-finite manifest value')


def verify_manifest(
    raw: bytes, signature_b64: str, public_key: bytes, *,
    repository: str, platform: str, agent_commit: str,
) -> dict:
    """Verify exact signed bytes, expected target, and the complete v1 schema.

    This is a pure parser. Callers must obtain the public key from a trusted
    independent configuration, not the manifest or a browser request.
    """
    try:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= 16384:
            raise ManifestError('Invalid manifest size')
        if not isinstance(signature_b64, str) or len(signature_b64) != 88:
            raise ManifestError('Invalid signature encoding')
        signature = base64.b64decode(signature_b64, validate=True)
        if len(signature) != 64 or len(public_key) != 32:
            raise ManifestError('Invalid signing identity')
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, raw)
        manifest = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_pairs,
                              parse_constant=_invalid_constant)
        if not isinstance(manifest, dict) or manifest.keys() != _KEYS:
            raise ManifestError('Unexpected manifest schema')
        if manifest['schema'] != 'hermes-webui-agent-image/v1':
            raise ManifestError('Unknown manifest version')
        if not isinstance(manifest['repository'], str) or not _REPO.fullmatch(manifest['repository']):
            raise ManifestError('Invalid image repository')
        if manifest['repository'] != repository:
            raise ManifestError('Image repository mismatch')
        if not isinstance(manifest['digest'], str) or not _DIGEST.fullmatch(manifest['digest']):
            raise ManifestError('Invalid image digest')
        if not isinstance(manifest['platform'], str) or manifest['platform'] not in ('linux/amd64', 'linux/arm64') or manifest['platform'] != platform:
            raise ManifestError('Platform mismatch')
        for name in ('agent_commit', 'webui_commit'):
            value = manifest[name]
            if not isinstance(value, str) or not _SHA.fullmatch(value):
                raise ManifestError('Invalid commit')
        if manifest['agent_commit'] != agent_commit:
            raise ManifestError('Agent commit mismatch')
        for name in ('agent_version', 'webui_version'):
            value = manifest[name]
            if not isinstance(value, str) or not _VERSION.fullmatch(value):
                raise ManifestError('Invalid release version')
        verification = manifest['verification']
        if not isinstance(verification, dict) or verification.keys() != _VERIFICATION_KEYS:
            raise ManifestError('Invalid compatibility receipt')
        if (verification['suite'] != 'agent-image-compatibility'
                or verification['passed'] is not True
                or not isinstance(verification['receipt_sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', verification['receipt_sha256'])):
            raise ManifestError('Unverified compatibility receipt')
        return manifest
    except (binascii.Error, InvalidSignature, UnicodeError, json.JSONDecodeError,
            TypeError, ValueError) as exc:
        raise ManifestError('Image manifest signature or schema invalid') from exc
