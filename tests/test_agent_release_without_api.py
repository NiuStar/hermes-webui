"""Agent release discovery must not depend on api.github.com."""
from unittest.mock import patch
import subprocess

from api import updates

RELEASE_HTML = '''<html><a href="/NousResearch/hermes-agent/releases/tag/v0.21.6">v0.21.6</a>
<a href="/NousResearch/hermes-agent/commit/818c13be1dc4fd28987e1e881a9408224afd4535">818c13b</a></html>'''


def test_official_release_page_links_commit(monkeypatch):
    monkeypatch.setattr(updates, '_agent_release_html', lambda: ('v0.21.6', RELEASE_HTML))
    assert updates._published_agent_release() == ('v0.21.6', '818c13be1dc4fd28987e1e881a9408224afd4535')


def test_release_page_missing_commit_fails_closed(monkeypatch):
    monkeypatch.setattr(updates, '_agent_release_html', lambda: ('v0.21.6', '<html>v0.21.6</html>'))
    with patch.object(updates.subprocess, 'run', side_effect=AssertionError('invalid page must not query git')):
        try:
            updates._published_agent_release()
        except ValueError:
            pass
        else:
            raise AssertionError('ambiguous release page accepted')


def test_ancestry_is_proven_by_git_objects_not_github_api(monkeypatch):
    calls = []
    def git(args, **kwargs):
        calls.append(args)
        if args[-2:] == ['rev-parse', 'FETCH_HEAD^{}']:
            return subprocess.CompletedProcess(args, 0, 'b' * 40 + '\n', '')
        if args[-3:] == ['cat-file', '-t', 'a' * 40]:
            return subprocess.CompletedProcess(args, 0, 'commit\n', '')
        if 'merge-base' in args:
            return subprocess.CompletedProcess(args, 0, '', '')
        return subprocess.CompletedProcess(args, 0, '', '')
    monkeypatch.setattr(updates.subprocess, 'run', git)
    monkeypatch.setattr(updates, '_agent_comparison_version', lambda _latest: 'v0.21.6')
    assert updates._agent_commit_comparison('a' * 40, 'b' * 40) == ('ahead', 1)
    assert all('api.github.com' not in ' '.join(a) for a in calls)


def test_diverged_git_history_fails_closed(monkeypatch):
    updates._agent_ancestry_cache.clear()
    def git(args, **kwargs):
        if args[-2:] == ['rev-parse', 'FETCH_HEAD^{}']:
            return subprocess.CompletedProcess(args, 0, 'b' * 40 + '\n', '')
        if args[-3:] == ['cat-file', '-t', 'a' * 40]:
            return subprocess.CompletedProcess(args, 0, 'commit\n', '')
        if 'merge-base' in args:
            return subprocess.CompletedProcess(args, 1, '', '')
        return subprocess.CompletedProcess(args, 0, '', '')
    monkeypatch.setattr(updates.subprocess, 'run', git)
    monkeypatch.setattr(updates, '_agent_comparison_version', lambda _latest: 'v0.21.6')
    assert updates._agent_commit_comparison('a' * 40, 'b' * 40) == ('unknown', 0)


def test_release_commit_mismatch_fails_closed(monkeypatch):
    monkeypatch.setattr(updates, '_published_agent_release', lambda: ('v0.21.6', 'a' * 40))
    try:
        updates._agent_comparison_version('b' * 40)
    except ValueError:
        pass
    else:
        raise AssertionError('changed Release commit accepted')
