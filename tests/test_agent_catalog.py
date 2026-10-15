"""Registry-backed Agent updates follow official releases without hand-written pins."""
import pytest
from api import docker_self_update as dsu

WEBUI='b'*40
AGENT='c'*40
REPO='24802117/hermes-webui'

def configured(monkeypatch):
    monkeypatch.setenv('HERMES_WEBUI_AGENT_CATALOG_ENABLED','1')
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_SELF_UPDATE','1')
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_IMAGE',REPO)
    live={'Id':'e'*64,'Image':'sha256:'+'f'*64,'Config':{'Labels':{}},'State':{'Running':True}}
    class Engine:
        pulls=[]
        def inspect(self,_):return live
        def inspect_image(self,_):return {'Config':{'Labels':{
            'org.opencontainers.image.revision':WEBUI,
            'org.opencontainers.image.version':'v2026.10.14-r1',
            'org.opencontainers.image.hermes-agent.path':'/opt/hermes',
            'org.opencontainers.image.hermes-agent.revision':'a'*40}}}
        def distribution(self,_):return {'Descriptor':{'digest':'sha256:'+'d'*64},
            'Platforms':[{'os':'linux','architecture':'amd64'}]}
        def pull(self,ref):self.pulls.append(ref)
    class Runtime:
        def _run(self,args):assert args==['docker','compose','version']
    engine=Engine();monkeypatch.setattr(dsu,'DockerEngine',lambda:engine)
    monkeypatch.setattr(dsu,'_compose_agent_preflight',lambda *_a,**_k:(Runtime(),REPO+'@sha256:'+'f'*64))
    return engine

def test_catalog_preflight_never_pulls(monkeypatch):
    import threading
    engine=configured(monkeypatch)
    result,worker=dsu._control_request({'action':'agent_catalog_preflight','agent_commit':AGENT,
        'agent_version':'v0.22.1','token':'known','image':'evil/latest'},busy=threading.Lock(),expected_token='known')
    assert result['ok'] and result['latest_sha']==AGENT and worker is None
    assert not engine.pulls

@pytest.mark.parametrize('state',['succeeded','rolled_back'])
def test_catalog_worker_pulls_only_resolved_digest_and_reports_rollback(monkeypatch,state):
    import threading
    engine=configured(monkeypatch)
    monkeypatch.setattr(dsu,'_apply_signed_compose_agent',lambda *_a,**_k:{'state':state})
    progress=dsu.UpdateProgress();busy=threading.Lock()
    result,worker=dsu._control_request({'action':'agent_catalog_update','agent_commit':AGENT,
        'agent_version':'v0.22.1','token':'known'},busy=busy,expected_token='known',progress=progress)
    assert result['ok'];worker.start();worker.join(5);assert not worker.is_alive()
    assert engine.pulls==[REPO+'@sha256:'+'d'*64]
    assert progress.snapshot(result['operation_id'])['state']==('succeeded' if state=='succeeded' else 'failed')
    assert not busy.locked()

def test_catalog_tag_binds_both_full_commits():
    from api.agent_image_catalog import image_tag
    assert image_tag(WEBUI, AGENT)==f'agent-{WEBUI}-{AGENT}'
    for bad in ['latest','c'*39,'../x','c'*40+'\n']:
        with pytest.raises(ValueError):image_tag(WEBUI,bad)

def test_catalog_resolves_registry_digest_without_pulling(monkeypatch):
    from api.agent_image_catalog import resolve
    class Engine:
        def distribution(self, ref):
            assert ref==f'{REPO}:agent-{WEBUI}-{AGENT}'
            return {'Descriptor':{'digest':'sha256:'+'d'*64},
                    'Platforms':[{'os':'linux','architecture':'amd64'}]}
        def pull(self,*_):pytest.fail('discovery may not pull')
    result=resolve(Engine(),repository=REPO,webui_commit=WEBUI,
        webui_version='v2026.10.14-r1',agent_commit=AGENT,agent_version='v0.22.1',platform='linux/amd64')
    assert result['image']==REPO+'@sha256:'+'d'*64
    assert result['agent_commit']==AGENT

@pytest.mark.parametrize('reply', [
    {'Descriptor':{'digest':'latest'},'Platforms':[{'os':'linux','architecture':'amd64'}]},
    {'Descriptor':{'digest':'sha256:'+'d'*64},'Platforms':[{'os':'linux','architecture':'arm64'}]},
    {},
])
def test_catalog_rejects_missing_digest_or_wrong_platform(reply):
    from api.agent_image_catalog import resolve
    class Engine:
        def distribution(self,_):return reply
    with pytest.raises(ValueError):
        resolve(Engine(),repository=REPO,webui_commit=WEBUI,webui_version='v2026.10.14-r1',
            agent_commit=AGENT,agent_version='v0.22.1',platform='linux/amd64')
