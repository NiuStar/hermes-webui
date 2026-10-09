"""Signed release tags and runtime code identity agree without weakening SHA checks."""
import os
import subprocess
import sys

from api import docker_self_update as dsu


def test_signed_agent_tag_matches_runtime_bare_version(tmp_path):
    revision = 'a' * 40
    baked, staged = tmp_path / 'baked', tmp_path / 'staged'
    for root in (baked, staged):
        (root / 'hermes_cli').mkdir(parents=True)
        (root / '.hermes-agent-revision').write_text(revision + '\n')
        (root / 'hermes_cli' / '__init__.py').write_text('')
        (root / 'hermes_cli' / 'config.py').write_text('')
        (root / 'hermes_cli' / 'version_info.py').write_text(
            f'def get_code_identity(refresh=False): return {{"sha": "{revision}", "version": "0.21.6"}}\n'
        )
        (root / 'run_agent.py').write_text('')
    env = {**os.environ, 'PYTHONPATH': str(staged)}
    def probe(sha, version):
        return subprocess.run([sys.executable, '-c', dsu._AGENT_RUNTIME_PROBE,
                               sha, version, str(baked), str(staged)],
                              cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15)
    assert probe(revision, 'v0.21.6').returncode == 0
    assert probe(revision, 'v0.21.5').returncode != 0
    assert probe('b' * 40, 'v0.21.6').returncode != 0
