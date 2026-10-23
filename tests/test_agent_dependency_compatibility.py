"""Approved compatibility is exact-release and exact-source, never blanket relaxation."""
import hashlib,json
import pytest
from scripts import prepare_agent_release as b

COMMIT='bf0203a794a07aac3ff764972ab9087baa4e4f4e'


def test_exact_approved_patch_preserves_original_and_records_delta(tmp_path,monkeypatch):
    original=b'[project]\ndependencies = ["Pillow==13.0.0; python_version >= \'3.14\'", "browser-harness==0.1.13; python_version >= \'3.14\'"]\n'
    source=tmp_path/'source';source.mkdir();(source/'pyproject.toml').write_bytes(original)
    monkeypatch.setattr(b,'V0225_PYPROJECT_SHA256',hashlib.sha256(original).hexdigest())
    evidence=b.apply_dependency_compatibility(source,COMMIT,'v0.22.5',tmp_path)
    assert (tmp_path/'upstream-pyproject.toml').read_bytes()==original
    assert (source/'pyproject.toml').read_bytes()==original.replace(b'Pillow==13.0.0',b'Pillow==12.3.0')
    assert evidence['upstream_sha256']==hashlib.sha256(original).hexdigest()
    assert evidence['patched_sha256']==hashlib.sha256((source/'pyproject.toml').read_bytes()).hexdigest()
    assert evidence['agent_commit']==COMMIT
    assert evidence['overrides']=={'Pillow':'12.3.0'}
    assert json.loads((source/'.hermes-compatibility.json').read_text())==evidence


def test_unapproved_release_is_unchanged(tmp_path):
    p=tmp_path/'pyproject.toml';p.write_text('Pillow==13.0.0')
    assert b.apply_dependency_compatibility(tmp_path,'c'*40,'v0.22.6',tmp_path) is None
    assert p.read_text()=='Pillow==13.0.0'


def test_changed_approved_source_refuses_before_write(tmp_path):
    p=tmp_path/'pyproject.toml';p.write_text('Pillow==13.0.0')
    with pytest.raises(ValueError,match='Approved dependency source mismatch'):
        b.apply_dependency_compatibility(tmp_path,COMMIT,'v0.22.5',tmp_path)
    assert p.read_text()=='Pillow==13.0.0'
    assert not (tmp_path/'upstream-pyproject.toml').exists()


@pytest.mark.parametrize('check_fails',[False,True])
def test_real_prepare_runs_dependency_gates_before_publication(tmp_path,monkeypatch,check_fails):
    original=b'Pillow==13.0.0'
    monkeypatch.setattr(b,'V0225_PYPROJECT_SHA256',hashlib.sha256(original).hexdigest())
    monkeypatch.setattr(b,'latest',lambda:('v0.22.5',COMMIT))
    monkeypatch.setattr(b,'prove_ancestry',lambda *_:None)
    monkeypatch.delenv('HERMES_AGENT_MANAGED_CLEANUP',raising=False)
    monkeypatch.setattr(b.shutil,'disk_usage',lambda _:type('D',(),{'free':10*1024**3})())
    monkeypatch.setattr(b,'download_archive',lambda _,p:p.write_bytes(b'archive'))
    class Tar:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def getmembers(self):return []
        def extractall(self,root,**_):
            source=root/('hermes-agent-'+COMMIT);source.mkdir();(source/'pyproject.toml').write_bytes(original)
    monkeypatch.setattr(b.tarfile,'open',lambda *_a,**_k:Tar())
    calls=[]
    def run(a,**_):
        calls.append(a)
        if a[:3]==['docker','image','inspect']:
            return json.dumps([{'Id':'sha256:'+'d'*64,'Os':'linux','Architecture':'amd64','RepoDigests':[],
              'Config':{'Labels':{'org.opencontainers.image.revision':'b'*40,
               'org.opencontainers.image.version':'v1.0.0','org.opencontainers.image.hermes-agent.revision':(COMMIT if ':agent-' in a[-1] else 'e'*40)}}}])
        if a[:2]==['docker','inspect']:return json.dumps([{'State':{'Running':True,'Health':{'Status':'healthy'}}}])
        if a[:3]==['docker','run','-d']:return 'f'*64
        if a[:2]==['docker','exec'] and 'check' in a:
            if check_fails:raise RuntimeError('DEPENDENCY_CHECK_FAILED')
        if a[:2]==['docker','exec'] and a[-2]=='-c':return json.dumps({'passed':True,'pillow':'12.3.0'})
        if a[:2]==['docker','exec'] and '/tmp/agent_release_tool_probe.py' in a:return json.dumps({'passed':True})
        if a[:3]==['docker','manifest','inspect']:return '{"schemaVersion":2}'
        return ''
    monkeypatch.setattr(b,'run',run)
    monkeypatch.setattr(b.subprocess,'run',lambda a,**_:type('R',(),
        {'returncode':1 if a[:3]==['docker','manifest','inspect'] else 0,'stderr':'manifest unknown'})())
    if check_fails:
        with pytest.raises(RuntimeError,match='DEPENDENCY_CHECK_FAILED'):
            b.prepare('24802117/hermes-webui@sha256:'+'a'*64,'24802117/hermes-webui',tmp_path,publish=True)
        assert not any(a[:2]==['docker','push'] for a in calls)
    else:
        r=b.prepare('24802117/hermes-webui@sha256:'+'a'*64,'24802117/hermes-webui',tmp_path,publish=True)
        assert r['dependency_compatibility']['overrides']=={'Pillow':'12.3.0'}
        assert r['dependency_probe']['passed']
        assert r['state']=='published'
        check=next(i for i,a in enumerate(calls) if 'check' in a)
        push=next(i for i,a in enumerate(calls) if a[:2]==['docker','push'])
        assert check<push
