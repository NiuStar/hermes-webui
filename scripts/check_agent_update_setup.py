#!/usr/bin/env python3
"""Read-only, fail-closed setup check for opt-in signed Agent updates.

This checks syntax, file safety and the independently pinned public key. It
cannot establish that a publisher or compatibility receipt is trustworthy.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.agent_image_manifest import ManifestError, verify_manifest


class SetupError(ValueError):
    pass


_FIELDS = (
    'HERMES_WEBUI_ACCEPTED_IMAGE', 'HERMES_WEBUI_DOCKER_IMAGE',
    'HERMES_WEBUI_AGENT_COMMIT', 'HERMES_WEBUI_AGENT_MANIFEST_FILE',
    'HERMES_WEBUI_AGENT_MANIFEST_SIGNATURE_FILE',
    'HERMES_WEBUI_AGENT_PUBLISHER_PUBKEY_FILE',
    'HERMES_WEBUI_AGENT_MANIFEST_PUBKEY_SHA256',
)


def _read_regular(path: str, limit: int) -> bytes:
    if not path.startswith('/'):
        raise SetupError('Release material must have an absolute path')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or not 0 < info.st_size <= limit:
                raise SetupError('Invalid release material mode, type or size')
            data = os.read(fd, limit + 1)
            if len(data) != info.st_size:
                raise SetupError('Release material changed during read')
            return data
        finally:
            os.close(fd)
    except OSError as exc:
        raise SetupError(f'Release material unavailable: {type(exc).__name__}') from exc


def verify_setup(env: dict[str, str]) -> dict[str, str]:
    if any(not env.get(key) for key in _FIELDS):
        raise SetupError('Agent updater opt-in requires all release settings')
    repository = env['HERMES_WEBUI_DOCKER_IMAGE']
    if not re.fullmatch(r'(?:[a-z0-9][a-z0-9._-]*(?::[0-9]{1,5})?/)?(?:[a-z0-9][a-z0-9._-]*/)*[a-z0-9][a-z0-9._-]*', repository):
        raise SetupError('Invalid Docker repository')
    image = env['HERMES_WEBUI_ACCEPTED_IMAGE']
    if not re.fullmatch(re.escape(repository) + r'@sha256:[0-9a-f]{64}', image):
        raise SetupError('Accepted image must be the exact repository and immutable digest')
    if not re.fullmatch(r'[0-9a-f]{40}', env['HERMES_WEBUI_AGENT_COMMIT']):
        raise SetupError('Agent commit must be complete')
    fingerprint = env['HERMES_WEBUI_AGENT_MANIFEST_PUBKEY_SHA256']
    if not re.fullmatch(r'[0-9a-f]{64}', fingerprint):
        raise SetupError('Public-key fingerprint must be complete')
    raw = _read_regular(env['HERMES_WEBUI_AGENT_MANIFEST_FILE'], 16384)
    signature = _read_regular(env['HERMES_WEBUI_AGENT_MANIFEST_SIGNATURE_FILE'], 128).decode('ascii').strip()
    public_key = base64.b64decode(
        _read_regular(env['HERMES_WEBUI_AGENT_PUBLISHER_PUBKEY_FILE'], 128).strip(),
        validate=True,
    )
    if len(public_key) != 32 or hashlib.sha256(public_key).hexdigest() != fingerprint:
        raise SetupError('Public key differs from independently approved fingerprint')
    machine = os.uname().machine
    if machine not in ('x86_64', 'aarch64'):
        raise SetupError('Unsupported Agent update platform')
    try:
        verify_manifest(raw, signature, public_key, repository=repository,
                        platform='linux/amd64' if machine == 'x86_64' else 'linux/arm64',
                        agent_commit=env['HERMES_WEBUI_AGENT_COMMIT'])
    except ManifestError as exc:
        raise SetupError('Agent image manifest failed signature or target verification') from exc
    return {'image': image, 'agent_commit': env['HERMES_WEBUI_AGENT_COMMIT']}


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, required=True,
                        help='Private environment file with KEY=value entries; no shell evaluation')
    args = parser.parse_args()
    env = {}
    try:
        for line in args.env_file.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, sep, value = line.partition('=')
            if not sep or key not in _FIELDS or key in env:
                raise SetupError('Unexpected or duplicate updater setting')
            env[key] = value
        result = verify_setup(env)
    except (OSError, UnicodeError, ValueError, binascii.Error) as exc:
        parser.exit(2, f'Agent updater setup blocked: {type(exc).__name__}: {exc}\n')
    print('Agent updater setup syntax and key pin verified for ' + result['image'])


if __name__ == '__main__':
    main()
