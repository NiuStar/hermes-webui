"""Executable Docker Compose adapter tests for an isolated Agent transaction."""
import json
from pathlib import Path
import pytest
from scripts.agent_compose_runtime import DockerComposeRuntime
from scripts.agent_compose_transaction import PinError


def test_runtime_uses_fixed_stack_and_target_without_shell(tmp_path, monkeypatch):
    p=tmp_path/'compose.yaml';p.write_text('services: {}\n')
    calls=[]
    def fake(cmd, **kwargs):
        calls.append(cmd)
        if cmd[-3:]==['config','--hash','webui']:
            return type('R',(),{'returncode':0,'stdout':'webui '+'a'*64+'\n','stderr':''})()
        return type('R',(),{'returncode':0,'stdout':'{}','stderr':''})()
    monkeypatch.setattr('scripts.agent_compose_runtime.subprocess.run',fake)
    r=DockerComposeRuntime([p],project='qa',service='webui',container='qa-webui',
                           repository='24802117/hermes-webui',agent_commit='b'*40,
                           agent_version='v0.21.7',agent_probe=lambda *_: None)
    assert r.hash()=='a'*64
    r.up()
    assert calls[0]==['docker','compose','--project-directory',str(tmp_path),'-f',str(p),'config','--hash','webui']
    assert calls[1]==['docker','compose','--project-directory',str(tmp_path),'-f',str(p),
                     'up','-d','--no-deps','--pull','never','webui']
    assert all(isinstance(x,list) for x in calls)


def test_runtime_refuses_unpinned_owner_or_path_outside_project(tmp_path):
    p=tmp_path/'compose.yaml';p.write_text('services: {}\n')
    with pytest.raises(PinError):
        DockerComposeRuntime([p],project='qa; rm -rf /',service='webui',container='qa-webui',
                             repository='24802117/hermes-webui',agent_commit='b'*40,
                             agent_version='v0.21.7')
    with pytest.raises(PinError):
        DockerComposeRuntime([tmp_path/'missing.yaml'],project='qa',service='webui',container='qa-webui',
                             repository='24802117/hermes-webui',agent_commit='b'*40,
                             agent_version='v0.21.7')


def test_runtime_rejects_wrong_live_owner_and_nonhealthy_restore(tmp_path,monkeypatch):
    p=tmp_path/'compose.yaml';p.write_text('services: {}\n')
    r=DockerComposeRuntime([p],project='qa',service='webui',container='qa-webui',
                           repository='24802117/hermes-webui',agent_commit='b'*40,
                           agent_version='v0.21.7',health_timeout=0)
    monkeypatch.setattr(r,'inspect',lambda:{'Id':'c'*64,'Image':'sha256:'+'d'*64,
        'State':{'Running':True,'Health':{'Status':'unhealthy'}},
        'Config':{'Labels':{'com.docker.compose.project':'foreign',
                            'com.docker.compose.service':'webui'}}})
    with pytest.raises(PinError):r.accept_restored({'Id':'c'*64,'Image':'sha256:'+'d'*64})
    with pytest.raises(PinError):r.accept({'Id':'c'*64,'Image':'sha256:'+'d'*64})


def test_candidate_acceptance_requires_runtime_agent_probe(tmp_path,monkeypatch):
    p=tmp_path/'compose.yaml';p.write_text('services: {}\n')
    r=DockerComposeRuntime([p],project='qa',service='webui',container='qa-webui',
                           repository='24802117/hermes-webui',agent_commit='b'*40,
                           agent_version='v0.21.7',health_timeout=0)
    ref='24802117/hermes-webui@sha256:'+'a'*64
    owner={'Id':'c'*64,'Image':'sha256:'+'d'*64,
           'State':{'Running':True,'Health':{'Status':'healthy'}},
           'Config':{'Labels':{'com.docker.compose.project':'qa',
                               'com.docker.compose.service':'webui'}}}
    monkeypatch.setattr(r,'inspect',lambda:owner)
    monkeypatch.setattr(r,'render',lambda _: {'services':{'webui':{'image':ref}}})
    monkeypatch.setattr(r,'image_info',lambda _: {'Id':owner['Image']})
    with pytest.raises(PinError,match='probe'):
        r.accept(owner)
    called=[]
    r.agent_probe=lambda actual,sha,version: called.append((actual['Id'],sha,version))
    r.accept(owner)
    assert called==[(owner['Id'],'b'*40,'v0.21.7')]


def test_reusable_probe_calls_existing_venv_identity_verifier(tmp_path,monkeypatch):
    from scripts.agent_compose_runtime import verify_runtime_agent
    seen=[]
    def fake(engine,identifier,commit,version):seen.append((identifier,commit,version))
    monkeypatch.setattr('api.docker_self_update._verify_imported_agent',fake)
    owner={'Id':'c'*64}
    verify_runtime_agent(owner,'b'*40,'v0.21.7')
    assert seen==[(owner['Id'],'b'*40,'v0.21.7')]


def test_runtime_requires_provisioned_probe_before_starting_compose(tmp_path,monkeypatch):
    p=tmp_path/'compose.yaml';p.write_text('services: {}\n')
    r=DockerComposeRuntime([p],project='qa',service='webui',container='qa-webui',
                           repository='24802117/hermes-webui',agent_commit='b'*40,
                           agent_version='v0.21.7')
    def forbidden(*_a,**_k):pytest.fail('must not mutate Docker before probe is configured')
    monkeypatch.setattr(r,'_stack',forbidden)
    with pytest.raises(PinError,match='probe'):
        r.up()


def test_runtime_render_preserves_full_ordered_stack(tmp_path,monkeypatch):
    first=tmp_path/'base.yaml';last=tmp_path/'override.yaml'
    first.write_text('services: {}\n');last.write_text('services: {}\n')
    candidate=tmp_path/'candidate.yaml';candidate.write_text('services: {}\n')
    seen=[]
    def fake(cmd,**kwargs):
        seen.append(cmd)
        return type('R',(),{'returncode':0,'stdout':json.dumps({'name':'qa','services':{}}),'stderr':''})()
    monkeypatch.setattr('scripts.agent_compose_runtime.subprocess.run',fake)
    r=DockerComposeRuntime([first,last],project='qa',service='webui',container='qa-webui',
                           repository='24802117/hermes-webui',agent_commit='b'*40,
                           agent_version='v0.21.7',agent_probe=lambda *_:None)
    r.render(candidate)
    assert seen[0]==['docker','compose','--project-directory',str(tmp_path),
                     '-f',str(first),'-f',str(candidate),'config','--format','json']
