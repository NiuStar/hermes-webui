"""Verify immutable, platform-bound release images before GitHub publication."""
import argparse
import json
import re
import subprocess
import hashlib
from datetime import date

REPOSITORY = '24802117/hermes-webui'


def validate_source(commit, version):
    if (not re.fullmatch(r'[0-9a-f]{40}', str(commit))
            or not re.fullmatch(r'v\d{4}\.\d{2}\.\d{2}-r[1-9]\d*', str(version))):
        raise ValueError('Invalid release source or version')
    date.fromisoformat(version[1:11].replace('.', '-'))


def validate_identity(value, commit, version):
    validate_source(commit, version)
    if not isinstance(value, dict):
        raise ValueError('Image identity must be an object')
    if (value.get('repository') != REPOSITORY or value.get('source_commit') != commit
            or value.get('version') != version or value.get('platform') != 'linux/amd64'
            or not re.fullmatch(r'[0-9a-f]{40}', str(commit))
            or not re.fullmatch(r'v\d{4}\.\d{2}\.\d{2}-r[1-9]\d*', str(version))
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', str(value.get('digest')))
            or not re.fullmatch(r'[0-9a-f]{40}', str(value.get('agent_commit')))
            or not re.fullmatch(r'v\d+\.\d+\.\d+', str(value.get('agent_version')))):
        raise ValueError('Release image identity mismatch')
    return value


def run(*argv, timeout=180):
    return subprocess.check_output(argv, text=True, timeout=timeout).strip()


def verify(commit, version):
    validate_source(commit, version)
    if run('git', 'rev-parse', 'HEAD') != commit:
        raise ValueError('Checkout is not the release source')
    paths = run('git', 'ls-tree', '-r', '--name-only', commit).splitlines()
    source_hashes = {}
    for path in paths:
        if path.startswith(('api/', 'static/')) or path in ('server.py', 'docker_init.bash'):
            data = subprocess.check_output(['git', 'show', commit + ':' + path], timeout=20)
            source_hashes[path] = hashlib.sha256(data).hexdigest()
    if not source_hashes:
        raise ValueError('Empty release source inventory')
    image = REPOSITORY + ':' + version
    run('docker', 'pull', image, timeout=600)
    info = json.loads(run('docker', 'image', 'inspect', image))[0]
    labels = info['Config']['Labels']
    digests = [s.split('@', 1)[1] for s in info['RepoDigests'] if s.startswith(REPOSITORY + '@')]
    if len(set(digests)) != 1:
        raise ValueError('Ambiguous image digest')
    identity = validate_identity({
        'repository': REPOSITORY, 'digest': digests[0],
        'source_commit': labels.get('org.opencontainers.image.revision'),
        'version': labels.get('org.opencontainers.image.version'),
        'platform': info['Os'] + '/' + info['Architecture'],
        'agent_commit': labels.get('org.opencontainers.image.hermes-agent.revision'),
        'agent_version': labels.get('org.opencontainers.image.hermes-agent.version'),
    }, commit, version)
    immutable = REPOSITORY + '@' + identity['digest']
    name = 'hermes-release-verify-' + commit[:12]
    cid = run('docker', 'run', '-d', '--name', name, '--restart=no',
              '-e', 'HERMES_WEBUI_STATE_DIR=/home/hermeswebui/.hermes/webui',
              '-e', 'HERMES_WEBUI_AUTH_MODE=none', immutable)
    try:
        import time
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            state = json.loads(run('docker', 'inspect', cid))[0]['State']
            if not state['Running']:
                raise RuntimeError('Release initialization exited')
            if state.get('Health', {}).get('Status') == 'healthy':
                break
            time.sleep(5)
        else:
            raise RuntimeError('Release initialization deadline')
        run('docker', 'cp', 'scripts/agent_release_tool_probe.py', cid + ':/tmp/release-tool-probe.py')
        run('docker', 'exec', cid, 'chmod', '644', '/tmp/release-tool-probe.py')
        probe = json.loads(run('docker', 'exec', '-u', '1024:1024', cid,
                              '/app/venv/bin/python3', '/tmp/release-tool-probe.py',
                              identity['agent_commit'], identity['agent_version'], timeout=240))
        if probe.get('passed') is not True:
            raise ValueError('Agent runtime probe failed')
        # OCI metadata alone cannot attest the served WebUI source/version.
        code = ('import sys;sys.path.insert(0,"/app");'
                'from api._version import __version__;'
                'assert __version__==' + repr(version))
        run('docker', 'exec', cid, '/app/venv/bin/python3', '-c', code)
        source_probe = ('import pathlib,json,hashlib;expected=' + repr(source_hashes)
                        + ';assert all(hashlib.sha256((pathlib.Path("/apptoo")/p).read_bytes()).hexdigest()==h for p,h in expected.items())')
        run('docker', 'exec', cid, '/app/venv/bin/python3', '-c', source_probe)
        identity['verified_source_files'] = len(source_hashes)
        identity['tool_loop'] = probe
        return identity
    finally:
        run('docker', 'rm', '-f', cid, timeout=60)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--commit', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    # Validate before constructing an image tag or invoking Docker.
    validate_identity({'repository': REPOSITORY, 'source_commit': args.commit,
                       'version': args.version, 'platform': 'linux/amd64',
                       'digest': 'sha256:' + '0'*64, 'agent_commit': '0'*40,
                       'agent_version': 'v0.0.0'}, args.commit, args.version)
    from pathlib import Path
    Path(args.output).write_text(json.dumps(verify(args.commit, args.version), indent=2) + '\n')
