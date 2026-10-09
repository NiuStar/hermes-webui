#!/usr/bin/env python3
"""Read-only acceptance gate: live image must survive the effective Compose stack.

Pass the *full ordered* Compose file list used to create the service. This does
not change Docker or accept an image; a human must separately pin the digest.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


class PinError(ValueError):
    pass


def _run_json(*args: str):
    return json.loads(subprocess.check_output(args, text=True, timeout=30))


def verify_pin(container: dict, image: dict, compose: dict, *, accepted: str,
               service: str, files: list[str]) -> dict:
    if '@sha256:' not in accepted or len(accepted.rsplit('@sha256:', 1)[1]) != 64:
        raise PinError('Accepted reference must contain a complete digest')
    labels = container.get('Config', {}).get('Labels') or {}
    observed_files = labels.get('com.docker.compose.project.config_files', '').split(',')
    if not files or [str(Path(p).resolve()) for p in observed_files] != [str(Path(p).resolve()) for p in files]:
        raise PinError('Effective Compose file stack differs from live owner')
    if labels.get('com.docker.compose.service') != service:
        raise PinError('Compose service differs from live owner')
    if compose.get('services', {}).get(service, {}).get('image') != accepted:
        raise PinError('Compose recreation would not use accepted digest')
    if container.get('Image') != image.get('Id'):
        raise PinError('Running image ID differs from accepted reference')
    if accepted not in (image.get('RepoDigests') or []):
        raise PinError('Accepted digest is not bound to local image')
    state = container.get('State') or {}
    if not state.get('Running') or (state.get('Health') or {}).get('Status') != 'healthy':
        raise PinError('Live owner is not healthy')
    return {'ok': True, 'container_id': container.get('Id'), 'image_id': image['Id'],
            'accepted': accepted, 'service': service}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', required=True)
    parser.add_argument('--service', required=True)
    parser.add_argument('--accepted', required=True)
    parser.add_argument('--compose-file', action='append', required=True)
    args = parser.parse_args()
    files = [str(Path(p).resolve()) for p in args.compose_file]
    try:
        container = _run_json('docker', 'inspect', args.container)[0]
        image = _run_json('docker', 'image', 'inspect', args.accepted)[0]
        command = ['docker', 'compose']
        for path in files:
            command += ['-f', path]
        compose = _run_json(*command, 'config', '--format', 'json')
        print(json.dumps(verify_pin(container, image, compose, accepted=args.accepted,
                                    service=args.service, files=files), sort_keys=True))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, KeyError) as exc:
        parser.exit(2, f'Compose pin gate blocked: {type(exc).__name__}: {exc}\n')


if __name__ == '__main__':
    main()
