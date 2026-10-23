"""Builder-owned lifecycle: bounded retry and exact, audited scratch cleanup."""
import json
import subprocess
import pytest
from scripts import prepare_agent_release as b


def test_transient_failure_retries_but_validation_never_does(monkeypatch):
    monkeypatch.setattr(b.time,'sleep',lambda _:None)
    attempts=[]
    def flaky():
        attempts.append(1)
        if len(attempts)<3:raise TimeoutError('temporary')
        return 'verified'
    assert b.retry_transport(flaky)=='verified' and len(attempts)==3
    attempts.clear()
    def invalid():attempts.append(1);raise ValueError('identity mismatch')
    with pytest.raises(ValueError):b.retry_transport(invalid)
    assert len(attempts)==1


def test_owned_scratch_reclaimed_only_after_receipt(tmp_path):
    attempt=tmp_path/'attempt';attempt.mkdir()
    source=attempt/('hermes-agent-'+'c'*40);source.mkdir();(source/'x').write_text('code')
    (attempt/'agent.tar.gz').write_bytes(b'archive')
    (attempt/'receipt.json').write_text(json.dumps({'state':'published','agent_commit':'c'*40}))
    assert b.cleanup_scratch(attempt,'c'*40)==['agent.tar.gz',source.name]
    assert not source.exists() and not (attempt/'agent.tar.gz').exists()
    assert (attempt/'receipt.json').exists()


def test_unverified_or_symlink_scratch_never_removed(tmp_path):
    source=tmp_path/('hermes-agent-'+'c'*40);source.mkdir()
    with pytest.raises(ValueError):b.cleanup_scratch(tmp_path,'c'*40)
    (tmp_path/'receipt.json').write_text(json.dumps({'state':'published','agent_commit':'d'*40}))
    with pytest.raises(ValueError):b.cleanup_scratch(tmp_path,'c'*40)
    assert source.exists()


def test_qa_cleanup_requires_exact_id_label_and_no_mounts(monkeypatch,tmp_path):
    cid='d'*64;calls=[]
    owner={'Id':cid,'Image':'sha256:'+'e'*64,'State':{'Status':'exited'},'Mounts':[],
      'Config':{'Labels':{'io.hermes.release-builder.attempt':'attempt-123'}}}
    def run(args,**_):
        calls.append(args)
        if args[:2]==['docker','inspect']:return json.dumps([owner])
        return ''
    monkeypatch.setattr(b,'run',run)
    monkeypatch.setattr(b.subprocess,'run',lambda *_a,**_k:type('R',(),{'returncode':0,'stdout':'qa log','stderr':''})())
    b.cleanup_qa(cid,'attempt-123',tmp_path)
    assert ['docker','rm',cid] in calls
    owner['Mounts']=[{'Destination':'/business'}];calls.clear()
    with pytest.raises(ValueError):b.cleanup_qa(cid,'attempt-123',tmp_path)
    assert not any(a[:2]==['docker','rm'] for a in calls)


def test_owned_image_retention_keeps_two_latest_and_referenced(monkeypatch,tmp_path):
    repo='24802117/hermes-webui';refs=[]
    for i in range(4):
        agent=str(i)*40;ref=repo+':agent-'+'a'*40+'-'+agent;refs.append(ref)
        p=tmp_path/(ref.split(':')[1]+'-20260101T00000'+str(i)+'Z');p.mkdir()
        f=p/'receipt.json';f.write_text(json.dumps({'state':'published','image':ref,
          'webui_commit':'a'*40,'agent_commit':agent,'image_id':'sha256:'+str(i)*64}))
        import os
        os.utime(f,(100+i,100+i))
    calls=[]
    monkeypatch.setattr(b,'run',lambda args,**_: 'container-id' if args[-1].endswith('1'*64) else '')
    def engine(args,**_):
        if args[:3]==['docker','image','inspect']:
            return type('R',(),{'returncode':0,'stdout':json.dumps([{'Id':'sha256:'+'0'*64}])})()
        calls.append(args);return type('R',(),{'returncode':0})()
    monkeypatch.setattr(b.subprocess,'run',engine)
    b.retire_old_images(tmp_path,repo,set())
    assert calls==[['docker','image','rm',refs[0]]]


def test_prepare_repeated_published_target_skips_git_and_reclaims_scratch(monkeypatch,tmp_path):
    monkeypatch.setenv('HERMES_AGENT_MANAGED_CLEANUP','1')
    monkeypatch.setattr(b,'latest',lambda:('v0.22.4','c'*40))
    monkeypatch.setattr(b,'prove_ancestry',lambda *_:pytest.fail('published target does not refetch Git'))
    a=tmp_path/('agent-'+'b'*40+'-'+'c'*40+'-20260101T000000Z');a.mkdir()
    (a/'receipt.json').write_text(json.dumps({'state':'published','agent_commit':'c'*40}))
    (a/'agent.tar.gz').write_bytes(b'payload')
    def run(args,**_):
        if args[:3]==['docker','image','inspect']:
            return json.dumps([{'Id':'sha256:'+'d'*64,'Os':'linux','Architecture':'amd64','RepoDigests':[],
             'Config':{'Labels':{'org.opencontainers.image.revision':'b'*40,'org.opencontainers.image.version':'v1.0.0',
              'org.opencontainers.image.hermes-agent.revision':('c' if ':agent-' in args[-1] else 'e')*40,
              'org.opencontainers.image.hermes-agent.version':'v0.22.4'}}}])
        return ''
    monkeypatch.setattr(b,'run',run)
    monkeypatch.setattr(b.subprocess,'run',lambda *_a,**_k:type('R',(),{'returncode':0})())
    assert b.prepare('24802117/hermes-webui@sha256:'+'a'*64,'24802117/hermes-webui',tmp_path,publish=True)['state']=='already_published'
    assert not (a/'agent.tar.gz').exists() and (a/'receipt.json').exists()


def test_receipted_prelabel_qa_cleanup_requires_exact_record(monkeypatch,tmp_path):
    cid='d'*64
    (tmp_path/'receipt.json').write_text(json.dumps({'state':'published','qa_container':cid,'image_id':'sha256:'+'e'*64}))
    owner={'Id':cid,'Image':'sha256:'+'e'*64,'State':{'Status':'exited'},'Mounts':[],'Config':{'Labels':{}}}
    calls=[]
    monkeypatch.setattr(b,'run',lambda a,**_:json.dumps([owner]) if a[:2]==['docker','inspect'] else calls.append(a) or '')
    monkeypatch.setattr(b.subprocess,'run',lambda *_a,**_k:type('R',(),{'returncode':0,'stdout':'audit','stderr':''})())
    b.cleanup_qa(cid,tmp_path.name,tmp_path)
    assert calls==[['docker','rm',cid]]
    owner['Image']='sha256:'+'f'*64;calls.clear()
    with pytest.raises(ValueError):b.cleanup_qa(cid,tmp_path.name,tmp_path)
    assert not calls


def test_download_failure_finalizes_receipt_and_scratch_via_real_prepare(monkeypatch,tmp_path):
    monkeypatch.setenv('HERMES_AGENT_MANAGED_CLEANUP','1')
    monkeypatch.setattr(b,'latest',lambda:('v0.22.4','c'*40))
    monkeypatch.setattr(b,'prove_ancestry',lambda *_:None)
    monkeypatch.setattr(b,'retire_old_images',lambda *_:None)
    monkeypatch.setattr(b.shutil,'disk_usage',lambda _:type('D',(),{'free':10*1024**3})())
    def run(a,**_):
        if a[:3]==['docker','image','inspect']:
            return json.dumps([{'Id':'sha256:'+'d'*64,'Os':'linux','Architecture':'amd64',
                'Config':{'Labels':{'org.opencontainers.image.revision':'b'*40,
                 'org.opencontainers.image.version':'v1.0.0','org.opencontainers.image.hermes-agent.revision':'e'*40}}}])
        return ''
    monkeypatch.setattr(b,'run',run)
    monkeypatch.setattr(b.subprocess,'run',lambda *_a,**_k:type('R',(),{'returncode':1,'stderr':'manifest unknown'})())
    def failed(commit,archive):
        archive.write_bytes(b'partial-source')
        raise TimeoutError('download failed')
    monkeypatch.setattr(b,'download_archive',failed)
    with pytest.raises(TimeoutError,match='download failed'):
        b.prepare('24802117/hermes-webui@sha256:'+'a'*64,'24802117/hermes-webui',tmp_path,publish=True)
    attempt=next(tmp_path.glob('agent-*'))
    assert json.loads((attempt/'receipt.json').read_text())['state']=='failed'
    assert not (attempt/'agent.tar.gz').exists()


def test_rehearsal_publish_is_rejected_before_external_effects(tmp_path):
    with pytest.raises(ValueError,match='Rehearsal cannot publish'):
        b.prepare('24802117/hermes-webui@sha256:'+'a'*64,'24802117/hermes-webui',tmp_path,publish=True,rehearse=True)


def test_real_prepare_preserves_tool_failure_when_stop_also_fails(monkeypatch,tmp_path):
    monkeypatch.setenv('HERMES_AGENT_MANAGED_CLEANUP','1')
    monkeypatch.setattr(b,'latest',lambda:('v0.22.4','c'*40))
    monkeypatch.setattr(b,'prove_ancestry',lambda *_:None)
    monkeypatch.setattr(b,'retire_old_images',lambda *_:None)
    monkeypatch.setattr(b.shutil,'disk_usage',lambda _:type('D',(),{'free':10*1024**3})())
    monkeypatch.setattr(b,'download_archive',lambda _,p:p.write_bytes(b'archive'))
    class Tar:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def getmembers(self):return []
        def extractall(self,root,**_):(root/('hermes-agent-'+'c'*40)).mkdir()
    monkeypatch.setattr(b.tarfile,'open',lambda *_a,**_k:Tar())
    def run(a,**_):
        if a[:3]==['docker','image','inspect']:
            return json.dumps([{'Id':'sha256:'+'d'*64,'Os':'linux','Architecture':'amd64',
              'Config':{'Labels':{'org.opencontainers.image.revision':'b'*40,
               'org.opencontainers.image.version':'v1.0.0','org.opencontainers.image.hermes-agent.revision':'e'*40}}}])
        if a[:2]==['docker','inspect']:
            return json.dumps([{'State':{'Running':True,'Health':{'Status':'healthy'}}}])
        if a[:3]==['docker','run','-d']:return 'f'*64
        if a[:2]==['docker','exec'] and '/tmp/agent_release_tool_probe.py' in a:raise RuntimeError('PRIMARY_TOOL_FAILURE')
        if a[:2]==['docker','stop']:raise RuntimeError('SECONDARY_STOP_FAILURE')
        return ''
    monkeypatch.setattr(b,'run',run)
    monkeypatch.setattr(b.subprocess,'run',lambda a,**_:type('R',(),
        {'returncode':1 if a[:3]==['docker','manifest','inspect'] else 0,'stderr':'manifest unknown'})())
    with pytest.raises(RuntimeError,match='PRIMARY_TOOL_FAILURE'):
        b.prepare('24802117/hermes-webui@sha256:'+'a'*64,'24802117/hermes-webui',tmp_path,publish=True)
    a=next(tmp_path.glob('agent-*'))
    assert (a/'cleanup-error.json').exists()
    assert json.loads((a/'receipt.json').read_text())['state']=='failed'


def test_interrupted_marked_attempt_recovered_but_historical_untouched(tmp_path):
    name='agent-'+'b'*40+'-'+'c'*40+'-20260101T000000Z'
    a=tmp_path/name;a.mkdir();(a/'agent.tar.gz.partial').write_bytes(b'partial')
    (a/'attempt-owner.json').write_text(json.dumps({'attempt':name,'agent_commit':'c'*40}))
    other=tmp_path/(name[:-1]+'1Z');other.mkdir();(other/'agent.tar.gz').write_bytes(b'old')
    b.reap_published_scratch(tmp_path)
    assert not (a/'agent.tar.gz.partial').exists()
    assert json.loads((a/'receipt.json').read_text())['error_type']=='InterruptedPreparation'
    assert (other/'agent.tar.gz').exists()
