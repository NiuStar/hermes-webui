"""Published WebUI discovery uses human Release HTML, never GitHub REST or Atom."""
import pytest
from api import updates


def page(*releases):
    return '<html>'+''.join(
        f'<section data-release-anchor="release-{name}"><a href="/NiuStar/hermes-webui/releases/tag/{name}">{name}</a>'
        +('<span class="Label">Pre-release</span>' if pre else '')
        +f'<a href="/NiuStar/hermes-webui/commit/{"a"*40}">commit</a>'
        +'<div class="markdown-body">notes</div></section>' for name,pre in releases)+'</html>'


class Response:
    def __init__(self,text):self.text=text
    def __enter__(self):return self
    def __exit__(self,*_):return False
    def read(self,*_):return self.text.encode()


def test_release_html_filters_channels_and_never_sends_token(monkeypatch):
    seen=[]
    def fetch(req,**_):
        seen.append(req)
        assert req.full_url=='https://github.com/NiuStar/hermes-webui/releases'
        assert req.get_header('Authorization') is None
        return Response(page(('v2026.10.15-r2',False),('exp-v0.54.0',True)))
    monkeypatch.setenv('HERMES_WEBUI_GITHUB_TOKEN','must-not-send-to-html')
    monkeypatch.setattr(updates.urllib.request,'urlopen',fetch)
    assert [x['name'] for x in updates._github_release_tags()]==['v2026.10.15-r2']
    assert [x['name'] for x in updates._github_release_tags(channel='experimental')]==['exp-v0.54.0']
    assert all('api.github.com' not in x.full_url for x in seen)


def test_baked_unknown_version_reports_latest_without_authorizing_update(tmp_path,monkeypatch):
    monkeypatch.setattr(updates.urllib.request,'urlopen',lambda *_a,**_k:Response(page(('v2026.10.15-r2',False))))
    monkeypatch.setattr(updates,'WEBUI_VERSION','867c2bdc')
    info=updates._check_repo(tmp_path,'webui')
    assert info['current_version']=='867c2bdc' and info['latest_version']=='v2026.10.15-r2'
    assert info['behind'] is None and info['deployment_online_update'] is False
    assert info['release_status']=='CURRENT_RELEASE_UNPUBLISHED' and info['compare_url'] is None


def test_source_tag_navigation_does_not_count_as_release(monkeypatch):
    monkeypatch.setattr(updates.urllib.request,'urlopen',lambda *_a,**_k:Response(
        '<a href="/NiuStar/hermes-webui/releases/tag/v9.0.0">bare tag</a>'+page(('v1.0.0',False))))
    assert [x['name'] for x in updates._github_release_tags()]==['v1.0.0']


def test_release_pagination_is_bounded_and_exact_repository(monkeypatch):
    seen=[]
    def fetch(req,**_):
        seen.append(req.full_url)
        if len(seen)==1:
            return Response(page(('v2.0.0',False))+'<a rel="next" href="/NiuStar/hermes-webui/releases?page=2">Next</a>')
        return Response(page(('v1.0.0',False))+'<a rel="next" href="https://evil.test/">Next</a>')
    monkeypatch.setattr(updates.urllib.request,'urlopen',fetch)
    with pytest.raises(ValueError,match='pagination'):
        updates._github_release_tags()
    assert seen==['https://github.com/NiuStar/hermes-webui/releases','https://github.com/NiuStar/hermes-webui/releases?page=2']


def test_explicit_empty_release_page_ends_history_without_hiding_latest(monkeypatch):
    seen=[]
    def fetch(req,**_):
        seen.append(req.full_url)
        if len(seen)==1:
            return Response(page(('v2.0.0',False))+'<a rel="next" href="/NiuStar/hermes-webui/releases?page=2">Next</a>')
        return Response('<h2>There aren’t any releases here</h2>')
    monkeypatch.setattr(updates.urllib.request,'urlopen',fetch)
    assert [x['name'] for x in updates._github_release_tags()]==['v2.0.0']
