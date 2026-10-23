"""Approved exact-release application of upstream's security override."""
import hashlib,json
import pytest
from scripts import prepare_agent_release as b

COMMIT='bf0203a794a07aac3ff764972ab9087baa4e4f4e'
ORIGINAL=b'''[project]
dependencies = ["Pillow==13.0.0", "browser-harness==0.1.13"]
[tool.uv]
override-dependencies = ["Pillow==13.0.0"]
'''
REPORT='Found 1 incompatibility\nThe package `browser-harness` requires `pillow==12.3.0`, but `13.0.0` is installed\n'


def test_exact_approved_override_preserves_original_and_records_delta(tmp_path,monkeypatch):
    source=tmp_path/'source';source.mkdir();(source/'pyproject.toml').write_bytes(ORIGINAL)
    monkeypatch.setattr(b,'V0225_PYPROJECT_SHA256',hashlib.sha256(ORIGINAL).hexdigest())
    evidence=b.apply_dependency_compatibility(source,COMMIT,'v0.22.5',tmp_path)
    assert (tmp_path/'upstream-pyproject.toml').read_bytes()==ORIGINAL
    assert (source/'pyproject.toml').read_bytes()==ORIGINAL
    assert evidence['upstream_sha256']==hashlib.sha256(ORIGINAL).hexdigest()
    assert evidence['source_unchanged'] is True
    assert evidence['agent_commit']==COMMIT
    assert evidence['overrides']=={'Pillow':'13.0.0'}
    assert (source/'.hermes-compatibility-overrides.txt').read_text()=='Pillow==13.0.0\n'
    assert json.loads((source/'.hermes-compatibility.json').read_text())==evidence


def test_unapproved_release_is_unchanged(tmp_path):
    p=tmp_path/'pyproject.toml';p.write_text('Pillow==13.0.0')
    assert b.apply_dependency_compatibility(tmp_path,'c'*40,'v0.22.6',tmp_path) is None
    assert p.read_text()=='Pillow==13.0.0'


def test_changed_approved_source_refuses_before_write(tmp_path):
    p=tmp_path/'pyproject.toml';p.write_text('Pillow==13.0.0')
    with pytest.raises(ValueError,match='Approved dependency source mismatch'):
        b.apply_dependency_compatibility(tmp_path,COMMIT,'v0.22.5',tmp_path)
    assert not (tmp_path/'upstream-pyproject.toml').exists()


@pytest.mark.parametrize('report',[REPORT.replace('Found 1 incompatibility','Found 2 incompatibilities'),REPORT+'The package `other` requires `missing`, but it is not installed\n',REPORT.replace('13.0.0','12.4.0'),REPORT.split('\n',1)[1],'unknown/truncated'])
def test_dependency_check_rejects_every_other_failure(report):
    with pytest.raises(RuntimeError,match='Unexpected dependency incompatibility'):
        b.validate_compatibility_check(1,report)


def test_dependency_check_accepts_only_expected_metadata_mismatch():
    b.validate_compatibility_check(1,REPORT)
    b.validate_compatibility_check(0,'All installed packages are compatible')


@pytest.mark.parametrize('check_fails',[False,True])
def test_real_prepare_runs_dependency_gates_before_publication(tmp_path,monkeypatch,check_fails):
    monkeypatch.setattr(b,'V0225_PYPROJECT_SHA256',hashlib.sha256(ORIGINAL).hexdigest())
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
            source=root/('hermes-agent-'+COMMIT);source.mkdir();(source/'pyproject.toml').write_bytes(ORIGINAL)
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
        if a[:2]==['docker','exec'] and a[-2]=='-c':return json.dumps({'passed':True,'pillow':'13.0.0'})
        if a[:2]==['docker','exec'] and '/tmp/agent_release_tool_probe.py' in a:return json.dumps({'passed':True})
        if a[:3]==['docker','manifest','inspect']:return '{"schemaVersion":2}'
        return ''
    monkeypatch.setattr(b,'run',run)
    def subprocess_run(a,**_):
        calls.append(a)
        if 'check' in a:return type('R',(),{'returncode':1,'stdout':REPORT if not check_fails else REPORT+'The package `other` requires `missing`\n','stderr':''})()
        return type('R',(),{'returncode':1 if a[:3]==['docker','manifest','inspect'] else 0,'stderr':'manifest unknown'})()
    monkeypatch.setattr(b.subprocess,'run',subprocess_run)
    if check_fails:
        with pytest.raises(RuntimeError,match='Unexpected dependency incompatibility'):
            b.prepare('24802117/hermes-webui@sha256:'+'a'*64,'24802117/hermes-webui',tmp_path,publish=True)
        assert not any(a[:2]==['docker','push'] for a in calls)
    else:
        r=b.prepare('24802117/hermes-webui@sha256:'+'a'*64,'24802117/hermes-webui',tmp_path,publish=True)
        assert r['dependency_compatibility']['overrides']=={'Pillow':'13.0.0'}
        assert r['dependency_probe']['passed'] and r['state']=='published'
        dockerfile=next(tmp_path.glob('agent-*/Dockerfile')).read_text()
        assert 'ENV UV_OVERRIDE=/opt/hermes/.hermes-compatibility-overrides.txt' in dockerfile
        assert next(i for i,a in enumerate(calls) if 'check' in a)<next(i for i,a in enumerate(calls) if a[:2]==['docker','push'])
