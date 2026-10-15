"""Build follower refuses unknown registry state and preserves immutable source identity."""
import json
from pathlib import Path
import pytest
from scripts import prepare_agent_release as builder

BASE='24802117/hermes-webui@sha256:'+'a'*64


def test_existing_catalog_pair_is_checked_without_build(tmp_path,monkeypatch):
    monkeypatch.setattr(builder,'latest',lambda:('v0.22.1','c'*40))
    monkeypatch.setattr(builder,'prove_ancestry',lambda *_:None)
    calls=[]
    def run(args,**kwargs):
        calls.append(args)
        if args[:3]==['docker','image','inspect']:
            newer=':agent-' in args[-1]
            return json.dumps([{'Id':'sha256:'+'d'*64,'Os':'linux','Architecture':'amd64',
                'RepoDigests':[BASE], 'Config':{'Labels':{
                'org.opencontainers.image.revision':'b'*40,
                'org.opencontainers.image.version':'v2026.10.14-r1',
                'org.opencontainers.image.hermes-agent.revision':('c' if newer else 'e')*40,
                'org.opencontainers.image.hermes-agent.version':'v0.22.1'}}}])
        return ''
    monkeypatch.setattr(builder,'run',run)
    monkeypatch.setattr(builder.subprocess,'run',lambda *a,**k:type('R',(),{'returncode':0,'stdout':'{}','stderr':''})())
    result=builder.prepare(BASE,'24802117/hermes-webui',tmp_path,publish=True)
    assert result['state']=='already_published'
    assert not any('build' in a or 'push' in a for a in calls)


def test_registry_unknown_does_not_start_build(tmp_path,monkeypatch):
    monkeypatch.setattr(builder,'latest',lambda:('v0.22.1','c'*40))
    monkeypatch.setattr(builder,'prove_ancestry',lambda *_:None)
    def run(args,**kwargs):
        if args[:3]==['docker','image','inspect']:
            return json.dumps([{'Id':'sha256:'+'d'*64,'Os':'linux','Architecture':'amd64','Config':{'Labels':{
                'org.opencontainers.image.revision':'b'*40,'org.opencontainers.image.version':'v2026.10.14-r1',
                'org.opencontainers.image.hermes-agent.revision':'e'*40}}}])
        assert args[:2]==['docker','pull'];return ''
    monkeypatch.setattr(builder,'run',run)
    monkeypatch.setattr(builder.subprocess,'run',lambda *a,**k:type('R',(),{'returncode':1,'stdout':'','stderr':'connection refused'})())
    with pytest.raises(RuntimeError,match='Registry availability unknown'):
        builder.prepare(BASE,'24802117/hermes-webui',tmp_path,publish=True)
    assert not (tmp_path/'last-result.json').exists()
