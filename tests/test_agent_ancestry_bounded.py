"""Release checks need only a bounded commit graph, not historical trees."""
from types import SimpleNamespace
import pytest
from api import official_agent_release as proof
from api import updates

CURRENT='a'*40
LATEST='b'*40


def test_proof_fetch_is_bounded_commit_only_and_never_lazy(monkeypatch):
    calls=[]
    def output(args, **kwargs):
        calls.append((args,kwargs))
        if 'rev-parse' in args:return LATEST+'\n'
        if 'cat-file' in args:return 'commit\n'
        return ''
    monkeypatch.setattr(proof.subprocess,'check_output',output)
    proof.prove_ancestry(CURRENT,LATEST,'v0.22.1')
    fetch=next((args,kw) for args,kw in calls if 'fetch' in args)
    assert '--depth=1024' in fetch[0]
    assert '--filter=tree:0' in fetch[0]
    assert not any(x.startswith('--shallow-since') for x in fetch[0])
    assert all(kw['env'].get('GIT_NO_LAZY_FETCH')=='1' for _,kw in calls)
    assert 'merge-base' in calls[-1][0]


def test_webui_uses_same_ancestry_proof_as_builder(monkeypatch):
    updates._agent_ancestry_cache.clear()
    monkeypatch.setattr(updates,'_agent_comparison_version',lambda _:'v0.22.1')
    calls=[]
    monkeypatch.setattr(proof,'prove_ancestry',lambda *args:calls.append(args))
    assert updates._agent_commit_comparison(CURRENT,LATEST)==('ahead',1)
    assert calls==[(CURRENT,LATEST,'v0.22.1')]


def test_insufficient_graph_never_becomes_positive_proof(monkeypatch):
    def output(args,**kwargs):
        if 'rev-parse' in args:return LATEST+'\n'
        if 'cat-file' in args:return ''
        return ''
    monkeypatch.setattr(proof.subprocess,'check_output',output)
    with pytest.raises(ValueError):proof.prove_ancestry(CURRENT,LATEST,'v0.22.1')
