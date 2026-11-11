"""Exercise the verifier orchestration with Git and private image/proc fixtures."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENT = {'repository': 'https://github.com/NousResearch/hermes-agent.git',
         'commit': '3b421578588243f920886e6fe79a6faa87ea026e', 'version': 'v0.23.1'}


def promotion():
    spec = importlib.util.spec_from_file_location('release_promotion', ROOT / 'scripts/release_promotion.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def valid():
    return {'repository': '24802117/hermes-webui', 'version': 'v2026.11.11-r1',
            'source_commit': 'a' * 40, 'digest': 'sha256:' + 'b' * 64,
            'platform': 'linux/amd64', 'agent_commit': AGENT['commit'],
            'agent_version': AGENT['version'], 'agent_repository': AGENT['repository']}


def test_exact_digest_and_source_are_required_for_promotion():
    m = promotion()
    v = valid()
    assert m.validate_identity(v, 'a' * 40, v['version']) == v
    for field, replacement in [('source_commit', 'd'*40), ('version', 'v2026.11.10-r1'),
                               ('digest', 'latest'), ('platform', 'linux/arm64'),
                               ('repository', 'attacker/image'), ('agent_commit', 'main')]:
        with pytest.raises(ValueError):
            m.validate_identity({**v, field: replacement}, 'a'*40, v['version'])


def test_invalid_identity_does_not_invoke_docker(monkeypatch):
    m = promotion()
    monkeypatch.setattr(m, 'run', lambda *a, **k: pytest.fail('Docker must not run'))
    with pytest.raises(ValueError):
        m.verify('main', 'v2026.11.11-r1')


def test_impossible_calendar_date_is_rejected():
    m = promotion()
    v = {**valid(), 'version': 'v2026.02.30-r1'}
    with pytest.raises(ValueError):
        m.validate_identity(v, 'a'*40, v['version'])


@pytest.fixture
def verifier(monkeypatch, tmp_path):
    """Run real Git and remote Python payloads; replace only Docker effects.

    Paths are mapped into a private image/procfs. Child credentials are recorded,
    not setuid, so these are offline contract tests, not Docker acceptance.
    """
    m = promotion()
    repo, image = tmp_path / 'source', tmp_path / 'image'
    repo.mkdir()
    sources = {
        'api/example.py': 'VALUE = 1\n',
        'api/config.py': 'import os,sys\nsys.path.append(os.environ["HERMES_WEBUI_AGENT_DIR"])\n',
        'api/__init__.py': '', 'api/_version.py': '__version__ = "v2026.11.11-r1"\n',
        'static/index.html': '<html>release</html>', 'server.py': '# server\n',
        'bootstrap.py': '# bootstrap\n', 'mcp_server.py': '# mcp\n',
        'scripts/startup.py': '# startup\n', 'requirements.txt': '# dependencies\n',
        'pyproject.toml': '# metadata\n', 'uv.lock': '# lock\n',
        'docker_init.bash': '#!/bin/bash\n# init\n',
        'deploy/release-agent.json': json.dumps(AGENT),
    }
    sources['api/docker_self_update.py'] = (ROOT / 'api/docker_self_update.py').read_text()
    sources['scripts/agent_release_tool_probe.py'] = '# tool loop fixture\n'
    for path, text in sources.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
    git('init', '-q')
    git('add', '.')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'source')
    commit = git('rev-parse', 'HEAD')
    for root in ('app', 'apptoo'):
        shutil.copytree(repo, image / root, ignore=shutil.ignore_patterns('.git'))
        (image / root / 'api/_version.py').write_text('__version__ = "v2026.11.11-r1"\n')
    shutil.copyfile(repo / 'docker_init.bash', image / 'hermeswebui_init.bash')
    for root in ('opt/hermes', 'app/hermes-agent-src'):
        agent = image / root
        (agent / 'hermes_cli').mkdir(parents=True)
        (agent / '.hermes-agent-revision').write_text(AGENT['commit'])
        (agent / 'hermes_cli/__init__.py').write_text('')
        (agent / 'hermes_cli/config.py').write_text('')
        (agent / 'hermes_cli/version_info.py').write_text(
            'def get_code_identity(refresh=False):\n return ' + repr(
                {'sha': AGENT['commit'], 'version': AGENT['version'][1:]}) + '\n')
        (agent / 'run_agent.py').write_text('# Agent\n')
    python = image / 'app/venv/bin/python3'
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    proc = image / 'proc/123'
    proc.mkdir(parents=True)
    (proc / 'cmdline').write_bytes(b'python\x00server.py\x00')
    (proc / 'stat').write_bytes(b'123 (python) ' + b'0 ' * 19 + b'12345 0\n')
    (proc / 'status').write_text('Name:\tpython\nUid:\t1024\t1024\t1024\t1024\nGid:\t1024\t1024\t1024\t1024\n')
    (proc / 'cwd').symlink_to(image / 'app', target_is_directory=True)
    (proc / 'exe').symlink_to(sys.executable)
    (proc / 'environ').write_bytes(('VIRTUAL_ENV=' + str(image / 'app/venv') + '\0PATH=' + os.environ['PATH'] + '\0HERMES_WEBUI_AGENT_DIR=' +
                                   str(image / 'opt/hermes') + '\0').encode())
    v = {**valid(), 'source_commit': commit}
    state = {'calls': [], 'tool_passes': True, 'payloads': [], 'running': True,
             'copied_probe': None, 'fail_copy': False, 'after_tool': lambda: None}
    real_run = m.run
    def translate(value):
        return re.sub(r'/(?:apptoo|app|opt/hermes|proc|hermeswebui_init\.bash)(?=[/\\\"\x27\s]|$)',
                      lambda match: str(image) + match[0], value)
    def run(*args, **kwargs):
        state['calls'].append(args)
        if args[0] == 'git':
            return real_run('git', '-C', str(repo), *args[1:], **kwargs)
        if args[:3] == ('docker', 'image', 'inspect'):
            return json.dumps([{'Config': {'Labels': {
                'org.opencontainers.image.revision': v['source_commit'],
                'org.opencontainers.image.version': v['version'],
                'org.opencontainers.image.hermes-agent.repository': v['agent_repository'],
                'org.opencontainers.image.hermes-agent.revision': v['agent_commit'],
                'org.opencontainers.image.hermes-agent.version': v['agent_version']}},
                'Os': 'linux', 'Architecture': 'amd64',
                'RepoDigests': [v['repository']+'@'+v['digest']]}])
        if args[:3] == ('docker', 'run', '-d'):
            return 'd'*64
        if args[:2] == ('docker', 'inspect'):
            return json.dumps([{'State': {'Running': state['running'], 'Health': {'Status': 'healthy'}}}])
        if args[:2] == ('docker', 'cp'):
            state['copied_probe'] = Path(args[2]).read_bytes()
            if state['fail_copy']:
                raise subprocess.CalledProcessError(1, args)
        if args[:2] == ('docker', 'exec') and '-c' in args:
            code = translate(args[args.index('-c') + 1])
            state['payloads'].append(code)
            prelude = ('import subprocess,json\n_original_run=subprocess.run\n'
                       'def _run(*a, **kw):\n'
                       ' identity={k:kw.pop(k) for k in ("user","group","extra_groups") if k in kw}\n'
                       ' with open(' + repr(str(tmp_path / 'children.jsonl')) + ',"a") as f: f.write(json.dumps(identity)+"\\n")\n'
                       ' return _original_run(*a, **kw)\nsubprocess.run=_run\n')
            return subprocess.check_output([sys.executable, '-c', prelude + code], text=True,
                                           cwd=image / 'app', timeout=15).strip()
        if args[:2] == ('docker', 'exec') and '/tmp/release-tool-probe.py' in args:
            state['after_tool']()
            return json.dumps({'passed': state['tool_passes']})
        return ''
    monkeypatch.setattr(m, 'run', run)
    monkeypatch.chdir(repo)
    state.update(module=m, version=v['version'], commit=commit, image=image, repo=repo,
                 identity=v, proc=proc, child_log=tmp_path / 'children.jsonl')
    return state


def verify(state):
    return state['module'].verify(state['commit'], state['version'])


def test_real_verifier_rejects_self_reported_unapproved_agent(verifier):
    verifier['identity']['agent_commit'] = 'f' * 40
    verifier['identity']['agent_version'] = 'v99.9.9'
    with pytest.raises(ValueError, match='Agent|identity'):
        verify(verifier)
    assert not any(call[:3] == ('docker', 'run', '-d') for call in verifier['calls'])


def assert_cleaned(state):
    assert state['calls'][-1] == ('docker', 'rm', '-f', 'd'*64)


def test_real_verifier_positive_control(verifier):
    result = verify(verifier)
    assert result['tool_loop']['passed'] is True
    assert result['verified_source_files'] >= 14
    launch = next(call for call in verifier['calls'] if call[:3] == ('docker', 'run', '-d'))
    assert launch[-1] == valid()['repository'] + '@' + valid()['digest']
    assert not any(arg in launch for arg in ('-p', '--publish', '-v', '--volume', '--mount', '--privileged'))
    assert '--restart=no' in launch
    assert_cleaned(verifier)


@pytest.mark.parametrize('target', [
    'app/server.py', 'app/api/example.py', 'app/static/index.html',
    'app/bootstrap.py', 'app/mcp_server.py', 'app/scripts/startup.py',
    'app/requirements.txt', 'app/pyproject.toml', 'app/uv.lock',
    'apptoo/bootstrap.py', 'apptoo/mcp_server.py', 'apptoo/scripts/startup.py',
    'apptoo/requirements.txt', 'hermeswebui_init.bash',
])
def test_real_verifier_rejects_tampered_runtime_bytes(verifier, target):
    (verifier['image'] / target).write_text('# tampered runtime bytes\n')
    with pytest.raises(subprocess.CalledProcessError):
        verify(verifier)
    assert_cleaned(verifier)


def test_real_verifier_allows_generated_version_not_source_version(verifier):
    (verifier['repo'] / 'api/_version.py').write_text('__version__ = "uncommitted"\n')
    subprocess.check_call(['git', 'add', 'api/_version.py'], cwd=verifier['repo'])
    subprocess.check_call(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                           'commit', '-qm', 'source version'], cwd=verifier['repo'])
    verifier['commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                                cwd=verifier['repo'], text=True).strip()
    verifier['identity']['source_commit'] = verifier['commit']
    assert verify(verifier)['tool_loop']['passed'] is True
    assert_cleaned(verifier)


def test_real_verifier_uses_committed_pin_not_mutable_checkout(verifier):
    verifier['identity']['agent_commit'] = 'f'*40
    verifier['identity']['agent_version'] = 'v99.9.9'
    (verifier['repo'] / 'deploy/release-agent.json').write_text(json.dumps(
        {**AGENT, 'commit': 'f'*40, 'version': 'v99.9.9'}))
    with pytest.raises(ValueError, match='Agent'):
        verify(verifier)


@pytest.mark.parametrize('field,value', [('agent_commit', 'f'*40),
                                        ('agent_version', 'v99.9.9'),
                                        ('agent_repository', 'https://attacker.invalid/agent.git')])
def test_real_verifier_rejects_each_unapproved_agent_field(verifier, field, value):
    verifier['identity'][field] = value
    with pytest.raises(ValueError, match='Agent'):
        verify(verifier)


def test_real_verifier_requires_tool_success(verifier):
    verifier['tool_passes'] = False
    with pytest.raises(ValueError, match='runtime probe failed'):
        verify(verifier)
    assert_cleaned(verifier)


@pytest.mark.parametrize('defect', ['root', 'saved-root', 'wrong-cwd', 'wrong-script',
                                  'no-server', 'ambiguous-server', 'shadow-agent',
                                  'staged-agent-drift'])
def test_real_verifier_attests_server_runtime_not_forced_opt_import(verifier, defect):
    proc, image = verifier['proc'], verifier['image']
    if defect in ('root', 'saved-root'):
        uid = '0\t0\t0\t0' if defect == 'root' else '1024\t1024\t0\t1024'
        (proc / 'status').write_text('Uid:\t' + uid + '\nGid:\t1024\t1024\t1024\t1024\n')
    elif defect == 'wrong-cwd':
        (proc / 'cwd').unlink()
        (proc / 'cwd').symlink_to(image / 'apptoo')
    elif defect == 'wrong-script':
        (proc / 'cmdline').write_bytes(b'python\0/elsewhere/server.py\0')
    elif defect == 'no-server':
        (proc / 'cmdline').write_bytes(b'python\0other.py\0')
    elif defect == 'ambiguous-server':
        shutil.copytree(proc, image / 'proc/456', symlinks=True)
    elif defect == 'shadow-agent':
        shadow = image / 'shadow'
        shutil.copytree(image / 'opt/hermes', shadow)
        with (proc / 'environ').open('ab') as handle:
            handle.write(('PYTHONPATH=' + str(shadow) + '\0').encode())
    elif defect == 'staged-agent-drift':
        (image / 'app/hermes-agent-src/run_agent.py').write_text('# modified\n')
    with pytest.raises(subprocess.CalledProcessError):
        verify(verifier)
    assert_cleaned(verifier)


def test_real_verifier_records_runtime_identity_and_child_credentials(verifier):
    result = verify(verifier)
    assert result['server_runtime']['uid'] == 1024
    assert result['server_runtime']['server_source'].endswith('/app/server.py')
    assert result['server_runtime']['agent_origin'].endswith('/opt/hermes/hermes_cli/__init__.py')
    children = [json.loads(line) for line in verifier['child_log'].read_text().splitlines()]
    assert children == [{'user': 1024, 'group': 1024, 'extra_groups': []}]
    assert_cleaned(verifier)


@pytest.mark.parametrize('failure', ['initialization', 'probe-copy', 'post-workload-drift'])
def test_real_verifier_cleans_on_external_stage_failure(verifier, failure):
    if failure == 'initialization':
        verifier['running'] = False
    elif failure == 'probe-copy':
        verifier['fail_copy'] = True
    else:
        verifier['after_tool'] = lambda: (verifier['image'] / 'app/server.py').write_text('# changed\n')
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        verify(verifier)
    assert_cleaned(verifier)


@pytest.mark.parametrize('root', ['app', 'apptoo'])
def test_real_verifier_rejects_wrong_generated_version(verifier, root):
    (verifier['image'] / root / 'api/_version.py').write_text('__version__ = "v99.9.9"\n')
    with pytest.raises(subprocess.CalledProcessError):
        verify(verifier)
    assert_cleaned(verifier)


def test_real_verifier_reads_source_and_probe_from_git_not_worktree(verifier):
    (verifier['repo'] / 'server.py').write_text('# dirty checkout\n')
    (verifier['repo'] / 'scripts/agent_release_tool_probe.py').write_text('# untrusted local probe\n')
    assert verify(verifier)['tool_loop']['passed'] is True
    assert verifier['copied_probe'] == b'# tool loop fixture\n'
    assert_cleaned(verifier)
