"""Build follower refuses unknown registry state and preserves immutable source identity."""
import json
from pathlib import Path
import pytest
from scripts import prepare_agent_release as builder

BASE='24802117/hermes-webui@sha256:'+'a'*64


def test_verifier_uses_pinned_compatible_updater_not_agent_candidate(monkeypatch):
    calls=[]
    verifier='24802117/hermes-webui@sha256:'+'f'*64
    def run(args,**kwargs):
        calls.append(args)
        if args[:3]==['docker','image','inspect']:
            return json.dumps([{'Id':'sha256:'+'9'*64,'Os':'linux','Architecture':'amd64',
                'Config':{'Labels':{'org.opencontainers.image.revision':'b'*40,
                 'org.opencontainers.image.version':'v2026.10.15-r2'}}}])
        return ''
    monkeypatch.setattr(builder,'run',run)
    result=builder.runtime_verifier(verifier,'24802117/hermes-webui','b'*40,
                                    'v2026.10.15-r2','linux/amd64')
    assert result=='sha256:'+'9'*64
    assert calls[0]==['docker','pull',verifier]
    with pytest.raises(ValueError,match='Immutable verifier'):
        builder.runtime_verifier('repo:latest','24802117/hermes-webui','b'*40,
                                 'v2026.10.15-r2','linux/amd64')
    with pytest.raises(ValueError,match='Verifier WebUI identity'):
        builder.runtime_verifier(verifier,'24802117/hermes-webui','c'*40,
                                 'v2026.10.15-r2','linux/amd64')


def test_source_permissions_under_private_service_umask(tmp_path):
    import os
    root=tmp_path/'agent';root.mkdir(mode=0o700)
    sub=root/'hermes_cli';sub.mkdir(mode=0o700)
    script=sub/'main.py';script.write_text('print(1)');script.chmod(0o600)
    executable=root/'helper';executable.write_text('#!/bin/sh\n');executable.chmod(0o700)
    for name in ('.hermes-agent-revision','install-stamp.json'):
        p=root/name;p.write_text('stamp');p.chmod(0o600)
    builder.normalize_source_permissions(root)
    assert root.stat().st_mode&0o777==0o755
    assert sub.stat().st_mode&0o777==0o755
    assert script.stat().st_mode&0o777==0o644
    assert executable.stat().st_mode&0o777==0o755
    assert all((root/n).stat().st_mode&0o777==0o644 for n in ('.hermes-agent-revision','install-stamp.json'))


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


def test_builder_failure_writes_target_bound_diagnostic_status(tmp_path,monkeypatch):
    from api.agent_preparation_status import read_status
    monkeypatch.setenv('HERMES_AGENT_PREPARATION_STATUS_DIR',str(tmp_path/'public'))
    monkeypatch.setattr(builder,'latest',lambda:('v0.22.3','c'*40))
    monkeypatch.setattr(builder,'prove_ancestry',lambda *_:None)
    def run(args,**_):
        if args[:3]==['docker','image','inspect']:
            return json.dumps([{'Id':'sha256:'+'d'*64,'Os':'linux','Architecture':'amd64',
                'Config':{'Labels':{'org.opencontainers.image.revision':'b'*40,
                'org.opencontainers.image.version':'v2026.10.15-r2',
                'org.opencontainers.image.hermes-agent.revision':'e'*40}}}])
        return ''
    monkeypatch.setattr(builder,'run',run)
    monkeypatch.setattr(builder.subprocess,'run',lambda *a,**k:type('R',(),{'returncode':1,'stderr':'manifest unknown'})())
    monkeypatch.setattr(builder.shutil,'disk_usage',lambda _:type('D',(),{'free':1})())
    with pytest.raises(RuntimeError,match='disk headroom'):
        builder.prepare(BASE,'24802117/hermes-webui',tmp_path,publish=True)
    assert read_status(tmp_path/'public','b'*40,'c'*40,'v0.22.3')=='IMAGE_FAILED'
