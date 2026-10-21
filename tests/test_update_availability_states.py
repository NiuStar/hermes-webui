"""Executable regressions for independent update availability (no lifecycle writes)."""
import json
import subprocess
import threading
from pathlib import Path

import pytest
from api import updates, docker_self_update as dsu
from tests.test_agent_catalog import configured, AGENT


@pytest.mark.parametrize('error,state',[
    (dsu.DockerEngineError('Docker API 404: manifest unknown'),'IMAGE_PENDING'),
    (dsu.DockerEngineError('Docker API 500: registry unavailable'),'REGISTRY_UNAVAILABLE'),
])
def test_catalog_distinguishes_absent_image_from_registry_failure(monkeypatch,error,state):
    engine=configured(monkeypatch)
    engine.distribution=lambda _: (_ for _ in ()).throw(error)
    result,worker=dsu._catalog_request({'action':'agent_catalog_preflight',
        'agent_commit':AGENT,'agent_version':'v0.22.3'},busy=threading.Lock(),progress=dsu.UpdateProgress())
    assert result['availability']==state
    assert result['ok'] is False and worker is None and not engine.pulls


def test_webui_missing_release_has_explicit_non_installable_status(monkeypatch):
    monkeypatch.setattr(updates,'WEBUI_VERSION','v2026.10.15-r2')
    monkeypatch.setattr(updates,'_github_release_tags',lambda **_:[{'name':'v2026.10.08-r1','sha':'a'*40}])
    result=updates._check_webui_published_release_update()
    assert result['release_status']=='CURRENT_RELEASE_UNPUBLISHED'
    assert result['behind'] is None and result['deployment_online_update'] is False
    assert 'error' in result  # Never turn unknown direction into success.


def test_agent_preflight_preserves_structured_failure(monkeypatch):
    monkeypatch.setattr(updates,'deployment_info',lambda:{'online_update':True})
    monkeypatch.setattr(updates,'request_agent_preflight',lambda:{'ok':False})
    monkeypatch.setattr(updates,'request_agent_catalog',lambda *_:{'ok':False,'availability':'IMAGE_PENDING'})
    status=updates.docker_agent_release_status(commit=AGENT,version='v0.22.3')
    assert status['availability']=='IMAGE_PENDING' and status['ok'] is False
    assert updates.docker_agent_release_ready(commit=AGENT,version='v0.22.3') is False


def test_preparation_evidence_only_refines_confirmed_image_absence(monkeypatch,tmp_path):
    from api.agent_preparation_status import write_status
    from tests.test_agent_catalog import WEBUI
    engine=configured(monkeypatch)
    monkeypatch.setenv('HERMES_WEBUI_AGENT_PREPARATION_DIR',str(tmp_path))
    write_status(tmp_path,WEBUI,AGENT,'v0.22.3','IMAGE_FAILED')
    engine.distribution=lambda _: (_ for _ in ()).throw(dsu.DockerEngineError('Docker API 404: manifest unknown'))
    result,_=dsu._catalog_request({'action':'agent_catalog_preflight','agent_commit':AGENT,
        'agent_version':'v0.22.3'},busy=threading.Lock(),progress=dsu.UpdateProgress())
    assert result['availability']=='IMAGE_FAILED'
    engine.distribution=lambda _: (_ for _ in ()).throw(dsu.DockerEngineError('Docker API 500: unavailable'))
    result,_=dsu._catalog_request({'action':'agent_catalog_preflight','agent_commit':AGENT,
        'agent_version':'v0.22.3'},busy=threading.Lock(),progress=dsu.UpdateProgress())
    assert result['availability']=='REGISTRY_UNAVAILABLE'


def test_preparation_status_expired_or_other_target_is_unknown(tmp_path,monkeypatch):
    from api import agent_preparation_status as status
    from tests.test_agent_catalog import WEBUI
    monkeypatch.setattr(status.time,'time',lambda:10000)
    status.write_status(tmp_path,WEBUI,AGENT,'v0.22.3','IMAGE_PREPARING')
    assert status.read_status(tmp_path,WEBUI,AGENT,'v0.22.3')=='IMAGE_PREPARING'
    assert status.read_status(tmp_path,WEBUI,AGENT,'v0.22.4') is None
    monkeypatch.setattr(status.time,'time',lambda:14000)
    assert status.read_status(tmp_path,WEBUI,AGENT,'v0.22.3') is None


def test_pending_install_result_expires_before_normal_release_cache(monkeypatch):
    monkeypatch.setattr(updates,'_update_cache',{'webui':None,'agent':{'availability':'IMAGE_PENDING'},
        'checked_at':1,'include_agent':True,'channel':'stable'})
    monkeypatch.setattr(updates,'_check_in_progress',False)
    monkeypatch.setattr(updates.time,'time',lambda:121)
    calls=[]
    monkeypatch.setattr(updates,'_check_repo',lambda _p,n,*_a,**_k: calls.append(n) or {'name':n,'behind':0})
    updates.check_for_updates(channel='stable')
    assert calls==['webui','agent']


def _js_function(source,name):
    import re
    m=re.search(r'(?:async )?function '+re.escape(name)+r'\(',source)
    assert m,name
    # Functions are top-level and end with unindented closing brace.
    end=source.index('\n}',m.start())+2
    return source[m.start():end]


def test_settings_check_keeps_agent_ready_despite_webui_warning():
    root=Path(__file__).resolve().parents[1]
    ui=(root/'static/ui.js').read_text();panels=(root/'static/panels.js').read_text()
    script='''
const nodes={btnCheckUpdatesNow:{},checkUpdatesLabel:{},checkUpdatesStatus:{style:{}},
checkUpdatesSpinner:{style:{}}};
global.$=(id)=>nodes[id]||null;global.t=(key)=>key;
let shown=null;global._showUpdateBanner=(d)=>{shown=d};
global.api=async()=>({webui:{no_git:true,release_based:true,behind:null,error:'missing release',release_status:'CURRENT_RELEASE_UNPUBLISHED'},
agent:{no_git:true,image_managed:true,behind:1,latest_version:'v0.22.3',deployment_online_update:true,availability:'READY'}});
'''
    script+='\n'.join(_js_function(ui,n) for n in ['_i18nUpdateText','_formatUpdateTargetStatus','_formatManualUpdateInstruction','_formatAgentImageUpdateInstruction','_formatUpdateCheckError'])
    script+='\n'+_js_function(panels,'checkUpdatesNow')
    script+='''
(async()=>{await checkUpdatesNow();const text=nodes.checkUpdatesStatus.textContent;
if(!text.includes('Agent')||!text.includes('WebUI'))throw Error(text);
if(text.includes('settings_update_no_git'))throw Error('false non-git warning: '+text);
if(!shown||shown.agent.deployment_online_update!==true)throw Error('Agent banner hidden');
console.log(text)})().catch(e=>{console.error(e);process.exit(1)});
'''
    result=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
