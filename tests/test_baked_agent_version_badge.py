"""Baked Agent versions are shown from their matching install stamp."""
import json

from api import updates


def test_baked_agent_version_detected_from_matching_stamp(tmp_path, monkeypatch):
    agent = tmp_path / 'agent'
    agent.mkdir()
    revision = 'a' * 40
    (agent / '.hermes-agent-revision').write_text(revision)
    (agent / 'install-stamp.json').write_text(json.dumps({
        'baseVersion': '0.21.6', 'commit': revision, 'source': 'docker',
    }))
    package = agent / 'hermes_cli'
    package.mkdir()
    (package / '__init__.py').write_text('__version__: str\n')
    monkeypatch.setattr(updates, '_AGENT_DIR', str(agent))
    monkeypatch.setattr(updates, '_describe_git_version', lambda _: None)
    monkeypatch.setattr(updates, '_detect_agent_version_from_gateway_health', lambda: None)
    assert updates._detect_agent_version() == '0.21.6'
    (agent / '.hermes-agent-revision').write_text('b' * 40)
    assert updates._detect_agent_version() == 'not detected'
    (agent / '.hermes-agent-revision').write_text(revision)
    (agent / 'install-stamp.json').write_text(json.dumps({
        'baseVersion': '0.21.6', 'commit': revision, 'source': 'unknown',
    }))
    assert updates._detect_agent_version() == 'not detected'
    (agent / 'install-stamp.json').write_text('{invalid')
    assert updates._detect_agent_version() == 'not detected'
