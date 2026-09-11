import json

from api import updates


def test_baked_unknown_version_still_reports_latest_release(tmp_path, monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps([
                {'tag_name': 'v2026.09.11-r8e', 'draft': False, 'prerelease': False},
            ]).encode()

    monkeypatch.setattr(updates.urllib.request, 'urlopen', lambda *args, **kwargs: FakeResponse())
    monkeypatch.setattr(updates, 'WEBUI_VERSION', '867c2bdc')
    info = updates._check_repo(tmp_path, 'webui')
    assert info['current_version'] == '867c2bdc'
    assert info['latest_version'] == 'v2026.09.11-r8e'
    assert info['behind'] == 1
    assert info['compare_url'] is None


def test_release_api_filters_stable_and_experimental(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps([
                {'tag_name': 'v2026.09.11-r8e', 'draft': False, 'prerelease': False},
                {'tag_name': 'exp-v0.54.0', 'draft': False, 'prerelease': True},
                {'tag_name': 'v9.0.0', 'draft': True, 'prerelease': False},
            ]).encode()

    monkeypatch.setattr(updates.urllib.request, 'urlopen', lambda *args, **kwargs: FakeResponse())
    assert [item['name'] for item in updates._github_release_tags(channel='stable')] == ['v2026.09.11-r8e']
    assert [item['name'] for item in updates._github_release_tags(channel='experimental')] == ['exp-v0.54.0']


def test_private_release_check_uses_bearer_token(monkeypatch):
    seen = {}
    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'[]'
    def fake_urlopen(request, timeout=0):
        seen['authorization'] = request.get_header('Authorization')
        return FakeResponse()
    monkeypatch.setenv('HERMES_WEBUI_GITHUB_TOKEN', 'private-test-token')
    monkeypatch.setattr(updates.urllib.request, 'urlopen', fake_urlopen)
    assert updates._github_release_tags() == []
    assert seen['authorization'] == 'Bearer private-test-token'
