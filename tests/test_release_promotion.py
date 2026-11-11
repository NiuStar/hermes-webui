"""Promotion authenticates a tested Docker artifact, not an arbitrary version tag."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def promotion():
    spec = importlib.util.spec_from_file_location('release_promotion', ROOT / 'scripts/release_promotion.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def valid():
    return {'repository': '24802117/hermes-webui', 'version': 'v2026.11.11-r1',
            'source_commit': 'a' * 40, 'digest': 'sha256:' + 'b' * 64,
            'platform': 'linux/amd64', 'agent_commit': 'c' * 40, 'agent_version': 'v0.23.1'}


def test_exact_digest_and_source_are_required_for_promotion():
    m = promotion()
    v = valid()
    assert m.validate_identity(v, 'a' * 40, 'v2026.11.11-r1') == v
    for field, replacement in [('source_commit', 'd'*40), ('version', 'v2026.11.10-r1'),
                               ('digest', 'latest'), ('platform', 'linux/arm64'),
                               ('repository', 'attacker/image'), ('agent_commit', 'main')]:
        with pytest.raises(ValueError):
            m.validate_identity({**v, field: replacement}, 'a'*40, 'v2026.11.11-r1')


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


@pytest.mark.parametrize('probe_passes', [True, False])
def test_real_verifier_requires_tool_success_and_cleans_its_container(monkeypatch, probe_passes):
    import json
    m = promotion()
    v = valid()
    calls = []
    def run(*args, **kwargs):
        calls.append(args)
        if args[:3] == ('git', 'rev-parse', 'HEAD'):return 'a'*40
        if args[:3] == ('git', 'ls-tree', '-r'):return 'api/example.py'
        if args[:3] == ('docker', 'image', 'inspect'):
            return json.dumps([{'Config': {'Labels': {
                'org.opencontainers.image.revision': v['source_commit'],
                'org.opencontainers.image.version': v['version'],
                'org.opencontainers.image.hermes-agent.revision': v['agent_commit'],
                'org.opencontainers.image.hermes-agent.version': v['agent_version']}},
                'Os': 'linux', 'Architecture': 'amd64',
                'RepoDigests': [v['repository']+'@'+v['digest']]}])
        if args[:3] == ('docker', 'run', '-d'):return 'd'*64
        if args[:2] == ('docker', 'inspect'):
            return json.dumps([{'State': {'Running': True, 'Health': {'Status': 'healthy'}}}])
        if '/tmp/release-tool-probe.py' in args and args[:2] == ('docker', 'exec'):
            return json.dumps({'passed': probe_passes})
        return ''
    monkeypatch.setattr(m, 'run', run)
    monkeypatch.setattr(m.subprocess, 'check_output', lambda *a, **k: b'example source')
    if probe_passes:
        result = m.verify('a'*40, v['version'])
        assert result['tool_loop']['passed'] is True
        assert result['verified_source_files'] == 1
    else:
        with pytest.raises(ValueError, match='runtime probe failed'):
            m.verify('a'*40, v['version'])
    assert calls[-1] == ('docker', 'rm', '-f', 'd'*64)
    assert any(v['repository']+'@'+v['digest'] in c for c in calls)
