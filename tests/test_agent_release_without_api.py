"""Agent release discovery must not depend on api.github.com."""
from unittest.mock import patch
import subprocess
import pytest

from api import updates

RELEASE_HTML = '''<html><a href="/NousResearch/hermes-agent/releases/tag/v0.21.6">v0.21.6</a>
<a href="/NousResearch/hermes-agent/commit/818c13be1dc4fd28987e1e881a9408224afd4535">818c13b</a></html>'''


def test_official_release_page_links_commit(monkeypatch):
    monkeypatch.setattr(updates, '_agent_release_html', lambda: ('v0.21.6', RELEASE_HTML))
    assert updates._published_agent_release() == ('v0.21.6', '818c13be1dc4fd28987e1e881a9408224afd4535')


def test_release_page_missing_commit_fails_closed(monkeypatch):
    monkeypatch.setattr(updates, '_agent_release_html', lambda: ('v0.21.6', '<html>v0.21.6</html>'))
    with patch.object(updates.subprocess, 'check_output', side_effect=AssertionError('invalid page must not query git')):
        with pytest.raises(ValueError):
            updates._published_agent_release()


def test_ancestry_is_proven_by_git_objects_not_github_api(monkeypatch):
    updates._agent_ancestry_cache.clear()
    calls = []
    def git(args, **kwargs):
        calls.append(args)
        if args[-2:] == ['rev-parse', 'FETCH_HEAD^{}']:
            return 'b' * 40 + '\n'
        if args[-3:] == ['cat-file', '-t', 'a' * 40]:
            return 'commit\n'
        return ''
    monkeypatch.setattr(updates.subprocess, 'check_output', git)
    monkeypatch.setattr(updates, '_agent_comparison_version', lambda _latest: 'v0.21.6')
    assert updates._agent_commit_comparison('a' * 40, 'b' * 40) == ('ahead', 1)
    assert any('merge-base' in a for a in calls)
    assert all('api.github.com' not in ' '.join(a) for a in calls)


def test_diverged_git_history_fails_closed(monkeypatch):
    updates._agent_ancestry_cache.clear()
    def git(args, **kwargs):
        if args[-2:] == ['rev-parse', 'FETCH_HEAD^{}']:
            return 'b' * 40 + '\n'
        if args[-3:] == ['cat-file', '-t', 'a' * 40]:
            return 'commit\n'
        if 'merge-base' in args:
            raise subprocess.CalledProcessError(1, args)
        return ''
    monkeypatch.setattr(updates.subprocess, 'check_output', git)
    monkeypatch.setattr(updates, '_agent_comparison_version', lambda _latest: 'v0.21.6')
    with pytest.raises(subprocess.CalledProcessError):
        updates._agent_commit_comparison('a' * 40, 'b' * 40)
    assert not updates._agent_ancestry_cache


def test_release_commit_mismatch_fails_closed(monkeypatch):
    monkeypatch.setattr(updates, '_published_agent_release', lambda: ('v0.21.6', 'a' * 40))
    with pytest.raises(ValueError):
        updates._agent_comparison_version('b' * 40)
