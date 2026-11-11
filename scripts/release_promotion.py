"""Verify immutable, platform-bound release images before GitHub publication.

Release authority is deploy/release-agent.json at the requested Git commit,
not candidate labels or working-tree files. Hash tracked runtime/dependency
inputs in both the baked and served trees, and the root init entrypoint;
validate generated version files separately. Observe the server in procfs,
then reproduce its venv imports as its non-root UID with its environment/cwd.
The bounded local tool loop is an additional behavioral check, not import
provenance: that historical probe deliberately forces the baked Agent tree.

This is isolated release acceptance only: no host state mounts, published
ports, production restart or publication. It is not in-process introspection
of an already-imported module or independent upstream Agent byte attestation.
"""
import argparse
import json
import re
import subprocess
import hashlib
import ast
import tempfile
from pathlib import Path
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


def source_bytes(commit, path):
    return subprocess.check_output(['git', 'show', commit + ':' + path], timeout=20)


def agent_pin(commit):
    # Git is the authority, not an edited checkout or the candidate's own labels.
    pin = json.loads(source_bytes(commit, 'deploy/release-agent.json'))
    if (not isinstance(pin, dict)
            or pin.get('repository') != 'https://github.com/NousResearch/hermes-agent.git'
            or not re.fullmatch(r'[0-9a-f]{40}', str(pin.get('commit')))
            or not re.fullmatch(r'v\d+\.\d+\.\d+', str(pin.get('version')))):
        raise ValueError('Invalid committed Agent release pin')
    return pin


def server_probe(commit, pin, version):
    # Reuse the existing baked/staged/import validator from the pinned source,
    # never the image's or a dirty checkout's implementation.
    tree = ast.parse(source_bytes(commit, 'api/docker_self_update.py'))
    probes = [ast.literal_eval(node.value) for node in tree.body
              if isinstance(node, ast.Assign) and any(
                  isinstance(target, ast.Name) and target.id == '_AGENT_RUNTIME_PROBE'
                  for target in node.targets)]
    if len(probes) != 1 or not isinstance(probes[0], str):
        raise ValueError('Missing source-pinned Agent runtime validator')
    child = ("import sys,pathlib,json\nimport api.config\n"
             "assert pathlib.Path(api.config.__file__).resolve() == pathlib.Path('/app/api/config.py')\n"
             "from api._version import __version__\nassert __version__ == " + repr(version) + '\n'
             + 'sys.argv = ' + repr(['release-runtime', pin['commit'], pin['version'],
                                     '/opt/hermes', '/app/hermes-agent-src']) + '\n'
             + probes[0] + "\nprint(json.dumps({'agent_origin': str(origin)}))\n")
    # Read restricted procfs links/environment as the observed service user,
    # rather than granting the verifier SYS_PTRACE or forcing an Agent root.
    launcher = 'child_code = ' + repr(child) + '''
import os, pathlib, sys
process = pathlib.Path(sys.argv[1])
app = pathlib.Path('/app')
cwd = process.joinpath('cwd').resolve(strict=True)
assert cwd == app, str(cwd)
args = [arg.decode() for arg in process.joinpath('cmdline').read_bytes().split(b'\\0') if arg]
assert len(args) == 2 and (cwd / args[1]).resolve(strict=True) == app / 'server.py', args
python = app / 'venv/bin/python3'
assert process.joinpath('exe').resolve(strict=True) == python.resolve(strict=True)
environment = dict(entry.decode().split('=', 1) for entry in
                   process.joinpath('environ').read_bytes().split(b'\\0') if entry)
assert environment.get('VIRTUAL_ENV') == str(app / 'venv')
assert not environment.get('PYTHONOPTIMIZE'), 'Optimized runtime cannot attest assertions'
os.chdir(cwd)
os.execve(str(python), [str(python), '-c', child_code], environment)
'''
    return 'launcher = ' + repr(launcher) + '''
import json, pathlib, subprocess
app = pathlib.Path('/app')
servers = []
for process in pathlib.Path('/proc').iterdir():
    if not process.name.isdecimal():
        continue
    try:
        args = process.joinpath('cmdline').read_bytes().split(b'\\0')
    except FileNotFoundError:
        continue
    args = [arg.decode() for arg in args if arg]
    if len(args) >= 2 and pathlib.Path(args[1]).name == 'server.py':
        servers.append((process, args))
assert len(servers) == 1, 'Expected exactly one running server'
process, args = servers[0]
before = process.joinpath('stat').read_bytes()
assert len(args) == 2 and pathlib.Path(args[0]).name.startswith('python'), args
fields = dict(line.split(':', 1) for line in process.joinpath('status').read_text().splitlines() if ':' in line)
uids, gids = ([int(value) for value in fields[key].split()] for key in ('Uid', 'Gid'))
assert len(uids) == 4 and len(set(uids)) == 1 and uids[0] > 0, uids
assert len(gids) == 4 and len(set(gids)) == 1 and gids[0] > 0, gids
# Bootstrap isolated, then exec with the service's actual environment/cwd/import
# resolution. No /opt/hermes sys.path or PYTHONPATH override is introduced.
result = subprocess.run([str(app / 'venv/bin/python3'), '-I', '-c', launcher, str(process)],
                        cwd=app, user=uids[0], group=gids[0], extra_groups=[],
                        capture_output=True, text=True, timeout=120, check=True)
identity = json.loads(result.stdout.splitlines()[-1])
# starttime (field 22), not CPU counters, identifies a still-live same process.
assert before.split(b') ', 1)[1].split()[19] == process.joinpath('stat').read_bytes().split(b') ', 1)[1].split()[19]
after = dict(line.split(':', 1) for line in process.joinpath('status').read_text().splitlines() if ':' in line)
assert all(after[key] == fields[key] for key in ('Uid', 'Gid'))
identity.update(uid=uids[0], gid=gids[0], server_source=str(app / 'server.py'), pid=int(process.name))
print(json.dumps(identity))
'''


def verify(commit, version):
    validate_source(commit, version)
    if run('git', 'rev-parse', 'HEAD') != commit:
        raise ValueError('Checkout is not the release source')
    pin = agent_pin(commit)
    paths = run('git', 'ls-tree', '-r', '--name-only', commit).splitlines()
    source_hashes = {}
    for path in paths:
        # Include startup, MCP, scripts and dependency inputs, not only the UI.
        # _version is generated by Docker and is checked against the release below.
        if path != 'api/_version.py' and (path.startswith(('api/', 'static/', 'scripts/', 'deploy/'))
                or '/' not in path and (path.endswith(('.py', '.sh', '.bash', '.ps1'))
                or path.startswith('requirements') or path in ('pyproject.toml', 'uv.lock'))):
            data = source_bytes(commit, path)
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
        'agent_repository': labels.get('org.opencontainers.image.hermes-agent.repository'),
    }, commit, version)
    if any(identity.get('agent_' + field) != pin[key] for field, key in
           (('repository', 'repository'), ('commit', 'commit'), ('version', 'version'))):
        raise ValueError('Image Agent identity does not match committed release pin')
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
        source_probe = ('import ast,pathlib,hashlib\nversion=' + repr(version)
                        + '\nexpected=' + repr(source_hashes) + '''
for root in (pathlib.Path('/apptoo'), pathlib.Path('/app')):
    for path, digest in expected.items():
        target = root / path
        assert target.is_file() and not target.is_symlink(), str(target)
        assert hashlib.sha256(target.read_bytes()).hexdigest() == digest, str(target)
    generated = root / 'api/_version.py'
    assert not generated.is_symlink()
    tree = ast.parse(generated.read_bytes())
    assert len(tree.body) == 1 and isinstance(tree.body[0], ast.Assign)
    assignment = tree.body[0]
    assert len(assignment.targets) == 1 and isinstance(assignment.targets[0], ast.Name)
    assert assignment.targets[0].id == '__version__' and ast.literal_eval(assignment.value) == version
entrypoint = pathlib.Path('/hermeswebui_init.bash')
assert not entrypoint.is_symlink()
assert hashlib.sha256(entrypoint.read_bytes()).hexdigest() == expected['docker_init.bash']
''')
        # Check source before importing runtime code, then again after workload.
        run('docker', 'exec', cid, '/app/venv/bin/python3', '-I', '-c', source_probe)
        runtime = json.loads(run('docker', 'exec', cid, '/app/venv/bin/python3', '-I', '-c',
                                 server_probe(commit, pin, version), timeout=180))
        with tempfile.TemporaryDirectory(prefix='release-verifier-') as directory:
            probe_file = Path(directory) / 'tool-probe.py'
            probe_file.write_bytes(source_bytes(commit, 'scripts/agent_release_tool_probe.py'))
            run('docker', 'cp', str(probe_file), cid + ':/tmp/release-tool-probe.py')
        run('docker', 'exec', cid, 'chmod', '644', '/tmp/release-tool-probe.py')
        probe = json.loads(run('docker', 'exec', '-u', str(runtime['uid']) + ':' + str(runtime['gid']), cid,
                              '/app/venv/bin/python3', '/tmp/release-tool-probe.py',
                              pin['commit'], pin['version'], timeout=240))
        if probe.get('passed') is not True:
            raise ValueError('Agent runtime probe failed')
        run('docker', 'exec', cid, '/app/venv/bin/python3', '-I', '-c', source_probe)
        identity['verified_source_files'] = len(source_hashes)
        identity['server_runtime'] = runtime
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
    validate_source(args.commit, args.version)

    Path(args.output).write_text(json.dumps(verify(args.commit, args.version), indent=2) + '\n')
