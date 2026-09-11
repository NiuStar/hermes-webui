"""Real Chromium + real HTTP + isolated SQLite. No route/response mocks."""
import json, os, socket, subprocess, time, urllib.request, uuid
from pathlib import Path
import pytest
from tests.test_application_v2 import runtime, ROOT


@pytest.mark.skipif(os.environ.get("APPLICATION_BROWSER_ACCEPTANCE") != "1", reason="explicit isolated real-browser acceptance")
def test_business_browser(test_server, runtime):
    from playwright.sync_api import sync_playwright
    service, config = runtime
    config_path=config.ledger_path.parent/'application.json'
    evidence=Path(os.environ['APPLICATION_BROWSER_EVIDENCE']);evidence.mkdir(parents=True,exist_ok=True)
    env=dict(x.split('=',1) for x in Path(f'/proc/{test_server.pid}/environ').read_text().split('\0') if '=' in x)
    env['APPLICATION_V2_CONFIG']=str(config_path)
    from tests.conftest import TEST_BASE
    # Complete first-run setup before booting browsers, avoiding a bootstrap/skip race.
    request=urllib.request.Request(TEST_BASE+'/api/onboarding/complete',data=b'{}',headers={'Content-Type':'application/json'},method='POST')
    with urllib.request.urlopen(request) as response: assert response.status==200
    password=uuid.uuid4().hex
    env['HERMES_WEBUI_PASSWORD']=password
    processes=[]; logs=[]; pages={}; checks=[]; errors=[]; requests=[]
    def check(name, condition):
        assert condition, name
        checks.append(name)
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True)
            contexts=[]
            for actor in ['creator','approver','publisher','observer']:
                with socket.socket() as s: s.bind(('127.0.0.1',0)); port=s.getsockname()[1]
                e=dict(env,HERMES_WEBUI_PORT=str(port),APPLICATION_V2_WEB_PRINCIPAL=actor)
                log=open(evidence/(actor+'.log'),'w');logs.append(log)
                proc=subprocess.Popen([env['HERMES_WEBUI_PYTHON'],str(ROOT/'server.py')],env=e,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT);processes.append(proc)
                url=f'http://127.0.0.1:{port}'
                for _ in range(100):
                    try:
                        with urllib.request.urlopen(url+'/health',timeout=1) as r: assert r.status==200
                        break
                    except Exception:
                        assert proc.poll() is None, actor+' server died'
                        time.sleep(.2)
                else: raise AssertionError(actor+' readiness')
                
                ctx=browser.new_context();contexts.append((actor,ctx));ctx.tracing.start(screenshots=True,snapshots=True)
                page=ctx.new_page();pages[actor]=page
                page.on('pageerror',lambda error: errors.append(str(error)))
                page.on('request',lambda r: requests.append({'method':r.method,'url':r.url}))
                page.goto(url,wait_until='domcontentloaded')
                page.locator('#pw').fill(password)
                page.locator('#login-form button[type=submit]').click()
                page.wait_for_url(url+'/',wait_until='domcontentloaded')
                cap=page.evaluate("async () => (await fetch('/api/application/tasks/capabilities')).json()")
                check(actor+' authenticated capabilities',cap['principal']==actor)
                # Dismiss first-run overlay through its normal persisted state, then enter actual panel.
                page.evaluate("skipOnboarding()")
                page.evaluate("switchPanel('applications',{fromRailClick:true})")
                page.locator('#applicationCandidateId').wait_for(state='visible')
            def form(actor, operation, fields):
                p=pages[actor]
                p.evaluate('newApplicationRequest()')
                p.locator('#applicationOperation').select_option(operation)
                if operation == 'recover': p.locator('#applicationEvidenceSha').fill('')
                for key,value in fields.items(): p.locator('#application'+key).fill(value)
                return p
            def submit(p):
                p.locator('#applicationSubmitBtn').click()
                p.wait_for_function("() => !_applicationSubmitting")
                return json.loads(p.locator('#applicationOutput').inner_text())
            cid=uuid.uuid4().hex;aid=uuid.uuid4().hex
            p=form('creator','create',{'CandidateId':cid});created=submit(p)
            check('create VERIFIED',created.get('result',{}).get('business_status')=='VERIFIED')
            task=p.locator('#applicationTaskId').input_value(); n=sum(r['method']=='POST' for r in requests)
            check('duplicate reads existing',submit(p)==created)
            check('duplicate no POST',sum(r['method']=='POST' for r in requests)==n)
            p.reload(wait_until='domcontentloaded');p.evaluate("switchPanel('applications')")
            check('refresh restores request and candidate',p.locator('#applicationTaskId').input_value()==task and p.locator('#applicationCandidateId').input_value()==cid)
            p.locator('#applicationCandidateId').fill(uuid.uuid4().hex)
            check('same ID changed request rejected',submit(p).get('issue',{}).get('code')=='CONFLICT')
            p=form('approver','approve',{'CandidateId':cid,'ApprovalId':aid,'ManifestSha':created['result']['manifest_sha'],'PolicySha':config.artifact_policy_sha,'Reference':'isolated-browser-functional-test-not-tool-release-approval'})
            approved=submit(p);check('approve durable',approved.get('result',{}).get('business_status')=='APPROVAL_DURABLE')
            p=form('publisher','publish',{'CandidateId':cid,'ApprovalId':aid});published=submit(p)
            check('publish unactivated',published.get('result',{}).get('business_status')=='PUBLISHED_UNACTIVATED')
            check('real published artifact exists',(service.files.published/cid).is_dir())
            p=form('observer','create',{'CandidateId':uuid.uuid4().hex});denied=submit(p)
            check('observer permission denied',denied.get('issue',{}).get('code')=='PERMISSION_DENIED')
            # Actual executor exits after durable create intent, before the file operation.
            interrupted=uuid.uuid4().hex;cid2=uuid.uuid4().hex
            code="""import os,json,sys
from api.application_runtime_config import load_config
from api.application_task_service import ApplicationService
s=ApplicationService(load_config(sys.argv[1]))
s.files.create=lambda *args,**kwargs: os._exit(73)
s.submit({'mode':'application_runtime_v2','task_id':sys.argv[2],'operation':'create','parameters':{'candidate_id':sys.argv[3]}},s.config.principal('creator'))
"""
            child=subprocess.run([env['HERMES_WEBUI_PYTHON'],'-c',code,str(config_path),interrupted,cid2],env=env,cwd=ROOT)
            check('real interrupted executor',child.returncode==73)
            p=form('creator','recover',{'CandidateId':cid2,'InterruptedTaskId':interrupted})
            p.locator('#applicationInspectBtn').click();p.wait_for_function("() => document.getElementById('applicationEvidenceSha').value.length===64")
            recovered=submit(p);check('recover succeeded',recovered.get('task_state')=='SUCCEEDED')
            check('recovery replay',submit(p)==recovered)
            # Verify both remaining recovery decisions through real browser forms.
            for decision in ['finalize_existing','resume_publish']:
                candidate=uuid.uuid4().hex; approval=uuid.uuid4().hex; original=uuid.uuid4().hex
                if decision=='resume_publish':
                    made=submit(form('creator','create',{'CandidateId':candidate}))
                    ok=submit(form('approver','approve',{'CandidateId':candidate,'ApprovalId':approval,'ManifestSha':made['result']['manifest_sha'],'PolicySha':config.artifact_policy_sha,'Reference':'isolated-recovery-browser'}))
                    check('recovery candidate independently approved',ok.get('task_state')=='SUCCEEDED')
                code2="""import os,sys
from api.application_runtime_config import load_config
from api.application_task_service import ApplicationService
s=ApplicationService(load_config(sys.argv[1]))
mode=sys.argv[5]
if mode=='finalize_existing':
    original=s.files.create
    def crash(*a,**k):
        original(*a,**k)
        os._exit(74)
    s.files.create=crash
    operation='create';actor='creator';params={'candidate_id':sys.argv[3]}
else:
    s.files.publish=lambda *a,**k: os._exit(74)
    operation='publish';actor='publisher';params={'candidate_id':sys.argv[3],'approval_id':sys.argv[4]}
s.submit({'mode':'application_runtime_v2','task_id':sys.argv[2],'operation':operation,'parameters':params},s.config.principal(actor))
"""
                child=subprocess.run([env['HERMES_WEBUI_PYTHON'],'-c',code2,str(config_path),original,candidate,approval,decision],env=env,cwd=ROOT)
                check(decision+' real executor exit',child.returncode==74)
                actor='creator' if decision=='finalize_existing' else 'publisher'
                p=form(actor,'recover',{'CandidateId':candidate,'InterruptedTaskId':original,'ApprovalId':approval if actor=='publisher' else ''})
                p.locator('#applicationInspectBtn').click()
                p.wait_for_function("() => document.getElementById('applicationEvidenceSha').value.length===64")
                check(decision+' inspection',p.locator('#applicationDecision').input_value()==decision)
                recovery=submit(p);check(decision+' recovery succeeds',recovery.get('task_state')=='SUCCEEDED')
                check(decision+' replay',submit(p)==recovery)
            p=form('creator','create',{'CandidateId':uuid.uuid4().hex});p.context.set_offline(True)
            offline=submit(p);check('offline error shown',bool(offline.get('issue')))
            saved=p.locator('#applicationTaskId').input_value();p.context.set_offline(False)
            check('explicit retry after reconnect',submit(p).get('task_state')=='SUCCEEDED')
            check('retry keeps request ID',p.locator('#applicationTaskId').input_value()==saved)
            p.context.clear_cookies()
            expired=submit(p)
            check('expired session structured error',expired.get('issue',{}).get('code')=='UNAUTHENTICATED')
            check('expired session draft preserved',p.locator('#applicationTaskId').input_value()==saved)
            url=p.url.split('/')[0]+'//'+p.url.split('/')[2]
            p.goto(url+'/login',wait_until='domcontentloaded');p.locator('#pw').fill(password)
            p.locator('#login-form button[type=submit]').click();p.wait_for_url(url+'/',wait_until='domcontentloaded')
            p.evaluate("switchPanel('applications')")
            check('relogin existing request reads back',submit(p).get('task_state')=='SUCCEEDED')
            p.set_viewport_size({'width':390,'height':844})
            p.locator('#applicationSubmitBtn').scroll_into_view_if_needed()
            check('mobile submit visible',p.locator('#applicationSubmitBtn').is_visible())
            check('mobile real create',submit(form('creator','create',{'CandidateId':uuid.uuid4().hex})).get('task_state')=='SUCCEEDED')
            p.screenshot(path=str(evidence/'mobile.png'),full_page=True)
            for actor,ctx in contexts:
                pages[actor].screenshot(path=str(evidence/(actor+'.png')),full_page=True)
                ctx.tracing.stop(path=str(evidence/(actor+'-trace.zip')))
            check('no uncaught JS errors',not errors)
            browser.close()
    finally:
        for proc in processes:
            proc.terminate()
            try: proc.wait(timeout=10)
            except subprocess.TimeoutExpired: proc.kill();proc.wait()
        for log in logs: log.close()
        (evidence/'result.json').write_text(json.dumps({'checks':checks,'errors':errors,'requests':requests,'children_stopped':all(p.poll() is not None for p in processes)},indent=2))
