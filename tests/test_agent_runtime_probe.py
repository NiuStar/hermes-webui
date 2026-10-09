"""The signed-image runtime probe reads executing code, not OCI labels."""
import os
import subprocess
import sys

import pytest

from api import docker_self_update as dsu


def _replace_with_symlink(baked, staged):
    target = staged / 'hermes_cli' / 'runtime.py'
    target.unlink()
    target.symlink_to(baked / 'hermes_cli' / 'runtime.py')


def _run_probe(tmp_path, *, mutate=None):
    baked = tmp_path / 'baked'
    staged = tmp_path / 'staged'
    for root in (baked, staged):
        (root / 'hermes_cli').mkdir(parents=True)
        (root / '.hermes-agent-revision').write_text('a' * 40 + '\n')
        (root / 'hermes_cli' / '__init__.py').write_text('VALUE = 1\n')
        (root / 'hermes_cli' / 'config.py').write_text('VALUE = 9\n')
        (root / 'hermes_cli' / 'runtime.py').write_text('VALUE = 2\n')
        (root / 'hermes_cli' / 'version_info.py').write_text(
            "def get_code_identity(refresh=False): return {'sha': '" + 'a' * 40 + "', 'version': '0.21.6'}\n"
        )
        (root / 'run_agent.py').write_text('')
    if mutate:
        mutate(baked, staged)
    cmd = [sys.executable, '-c', dsu._AGENT_RUNTIME_PROBE,
           'a' * 40, 'v0.21.6', str(baked), str(staged)]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                          cwd=tmp_path, env={**os.environ, 'PYTHONPATH': str(staged)})


def test_probe_detects_imported_source_and_matching_code(tmp_path):
    result = _run_probe(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ''


@pytest.mark.parametrize('mutate', [
    lambda baked, staged: (staged / '.hermes-agent-revision').write_text('b' * 40 + '\n'),
    lambda baked, staged: (staged / 'hermes_cli' / 'runtime.py').write_text('VALUE = 3\n'),
    lambda baked, staged: (staged / 'hermes_cli' / 'runtime.py').unlink(),
    lambda baked, staged: (staged / 'hermes_cli' / '__init__.py').write_text('VALUE = 3\n'),
    lambda baked, staged: (staged / 'hermes_cli' / 'extra.py').write_text('VALUE = 4\n'),
    _replace_with_symlink,
])
def test_probe_rejects_stale_or_different_code(tmp_path, mutate):
    assert _run_probe(tmp_path, mutate=mutate).returncode != 0


def test_probe_engine_requires_exit_zero_and_uses_exact_revision():
    class Engine:
        def __init__(self, code): self.code = code; self.commands = []
        def request(self, method, path, body=None, **_kw):
            if path.endswith('/exec'):
                self.commands.append(body)
                return {'Id': 'a' * 64}
            if path == '/exec/' + 'a' * 64 + '/start': return None
            if path == '/exec/' + 'a' * 64 + '/json': return {'Running': False, 'ExitCode': self.code}
            raise AssertionError(path)
    for code in (0, 1, None, False):
        engine = Engine(code)
        if type(code) is int and code == 0:
            dsu._verify_imported_agent(engine, 'candidate', 'a' * 40, 'v0.21.6')
        else:
            with pytest.raises(dsu.DockerEngineError, match='runtime Agent'):
                dsu._verify_imported_agent(engine, 'candidate', 'a' * 40, 'v0.21.6')
        assert engine.commands[0]['Cmd'][-4] == 'a' * 40
        assert engine.commands[0]['Cmd'][-3] == 'v0.21.6'


def test_probe_rejects_import_outside_baked_or_staged_source(tmp_path):
    baked = tmp_path / 'baked'
    staged = tmp_path / 'staged'
    external = tmp_path / 'external'
    for root in (baked, staged):
        (root / 'hermes_cli').mkdir(parents=True)
        (root / '.hermes-agent-revision').write_text('a' * 40 + '\n')
        (root / 'hermes_cli' / '__init__.py').write_text('VALUE = 1\n')
    (external / 'hermes_cli').mkdir(parents=True)
    (external / 'hermes_cli' / '__init__.py').write_text('VALUE = 1\n')
    result = subprocess.run(
        [sys.executable, '-c', dsu._AGENT_RUNTIME_PROBE,
         'a' * 40, 'v0.21.6', str(baked), str(staged)],
        capture_output=True, text=True, cwd=tmp_path,
        env={**os.environ, 'PYTHONPATH': str(external)}, timeout=15,
    )
    assert result.returncode != 0


def test_signed_replacement_probe_failure_rolls_back(monkeypatch):
    image = {'Id': 'sha256:' + 'f' * 64,
             'RepoDigests': ['repo/webui@sha256:' + 'd' * 64],
             'Os': 'linux', 'Architecture': 'amd64',
             'Config': {'Labels': {
                 'org.opencontainers.image.version': 'v1.2.3',
                 'org.opencontainers.image.revision': 'b' * 40,
                 'org.opencontainers.image.hermes-agent.revision': 'a' * 40,
                 'org.opencontainers.image.hermes-agent.version': 'v0.21.6',
                 'org.opencontainers.image.hermes-agent.path': '/opt/hermes',
             }}}
    old = {'Name': '/candidate', 'Id': 'a' * 64, 'Image': 'sha256:' + 'e' * 64,
           'State': {'Running': True},
           'HostConfig': {'Binds': [], 'Mounts': []},
           'Config': {'Env': ['HERMES_WEBUI_AGENT_DIR=/opt/hermes', 'PYTHONPATH=/opt/hermes']},
           'Mounts': []}
    class Engine:
        def __init__(self): self.calls = []; self.started = False; self.restored = False; self.created = False
        def pull(self, _): self.calls.append('pull')
        def inspect_image(self, ref):
            if ref == old['Image']:
                return {'Id': ref, 'Config': {'Labels': {
                    'org.opencontainers.image.version': 'v1.2.3',
                    'org.opencontainers.image.revision': 'b' * 40,
                }}}
            return image
        def inspect(self, name):
            if name == 'b' * 64 and self.created:
                return {**old, 'Id': 'b' * 64, 'State': {
                    'Running': self.started, 'Health': {'Status': 'healthy'}}}
            if name == 'a' * 64:
                return {**old, 'State': {'Running': self.started if self.restored else False,
                                         'Health': {'Status': 'healthy'}}}
            if name in ('candidate.hermes-update-old', 'candidate.hermes-update-new') and not self.created:
                raise dsu.DockerEngineError('not found')
            if name == 'candidate' and self.started and not self.restored:
                return {**old, 'Id': 'b' * 64, 'State': {'Running': True, 'Health': {'Status': 'healthy'}}}
            if name == 'candidate' and self.restored:
                return {**old, 'State': {'Running': self.started, 'Health': {'Status': 'healthy'}}}
            if name == 'candidate.hermes-update-old':
                return {**old, 'State': {'Running': False}}
            if name == 'candidate.hermes-update-new' and self.created:
                return {'Id': 'b' * 64}
            return old
        def rename(self, src, dst):
            if src == old['Id'] and dst == 'candidate':
                self.restored = True
            self.calls.append(('rename', src, dst))
        def stop(self, _): self.calls.append('stop')
        def create(self, *_):
            self.created = True; self.calls.append('create')
            return {'Id': 'b' * 64}
        def start(self, _): self.started = True; self.calls.append('start')
        def remove(self, name, **_kwargs):
            self.started = False
            self.calls.append(('remove', name))
    engine = Engine()
    monkeypatch.setattr(dsu, 'DockerEngine', lambda: engine)
    monkeypatch.setattr(dsu, '_verify_runtime_contract', lambda *_a, **_k: None)
    monkeypatch.setattr(dsu, '_verify_imported_agent', lambda *_a: (_ for _ in ()).throw(dsu.DockerEngineError('runtime Agent invalid')))
    expected = {'image': 'repo/webui@sha256:' + 'd' * 64,
                'platform': 'linux/amd64', 'webui_commit': 'b' * 40,
                'agent_commit': 'a' * 40, 'agent_version': 'v0.21.6',
                'webui_version': 'v1.2.3'}
    with pytest.raises(dsu.DockerEngineError, match='runtime Agent invalid'):
        dsu.replace_container('candidate', expected['image'], expected_agent_image=expected)
    assert ('rename', old['Id'], 'candidate') in engine.calls
    assert engine.calls.count('start') == 2
    assert ('remove', 'b' * 64) in engine.calls
