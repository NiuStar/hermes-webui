"""Real isolated WebUI + deterministic gateway: copy without reload.

COPY_TEST_BASELINE=1 serves the pinned pre-fix CSS without editing the tree.
COPY_TEST_MOBILE=1 tests Chromium touch at 390x844, not a physical iOS device.
CSP is bypassed for driver polling; production login/deployment is not covered.
"""
import hashlib,json,os,pathlib,sys,tempfile,subprocess
from playwright.sync_api import sync_playwright
from browser_conversation_lifecycle import DeterministicGateway,_start_webui_server,_terminate_process,FINAL_TEXT


def main():
 repo=pathlib.Path(__file__).resolve().parents[1]
 root=pathlib.Path(tempfile.mkdtemp(prefix='copy-stream-'))
 (root/'state'/'sessions').mkdir(parents=True)
 home=root/'home';home.mkdir();agent=root/'agent';agent.mkdir();(agent/'run_agent.py').write_text('')
 workspace=root/'workspace';workspace.mkdir()
 env={k:v for k,v in os.environ.items() if not any(x in k.upper() for x in ('TOKEN','PASSWORD','SECRET','API_KEY'))}
 env.update(HERMES_WEBUI_HOST='127.0.0.1',HERMES_WEBUI_STATE_DIR=str(root/'state'),HERMES_HOME=str(home),HERMES_BASE_HOME=str(home),HERMES_CONFIG_PATH=str(home/'config.yaml'),HERMES_WEBUI_SKIP_ONBOARDING='1',HERMES_WEBUI_AUTH_MODE='none',HERMES_WEBUI_AGENT_DIR=str(agent),HERMES_WEBUI_DEFAULT_WORKSPACE=str(workspace),HERMES_WEBUI_CHAT_BACKEND='gateway',HERMES_WEBUI_GATEWAY_USE_RUNS_API='1',NO_PROXY='127.0.0.1,localhost',no_proxy='127.0.0.1,localhost')
 gateway=DeterministicGateway('normal');gateway.start();gateway.release_settle.set();gateway.release_terminal.set();env['HERMES_WEBUI_GATEWAY_BASE_URL']=gateway.base_url
 proc=log=None
 try:
  subprocess.run([sys.executable,'-c','from api.models import Session;import os;s=Session(session_id="copybrowser01",title="Copy test",workspace=os.environ["HERMES_WEBUI_DEFAULT_WORKSPACE"],messages=[{"role":"user","content":"Earlier question"},{"role":"assistant","content":"Earlier answer"}]);s.save()'],cwd=repo,env=env,check=True)
  proc,log,_,url=_start_webui_server(repo,env,root)
  with sync_playwright() as p:
   browser=p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
   mobile=os.environ.get('COPY_TEST_MOBILE')=='1'
   context=browser.new_context(base_url=url,bypass_csp=True,service_workers='block',permissions=['clipboard-read','clipboard-write'],has_touch=mobile,is_mobile=mobile,viewport={'width':390,'height':844} if mobile else {'width':1280,'height':900})
   page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
   if os.environ.get('COPY_TEST_BASELINE')=='1':
    baseline=subprocess.check_output(['git','show','24e4e09582943b01bd3c89d6924e18284a1f907a:static/style.css'],cwd=repo)
    page.route('**/static/style.css*',lambda route:route.fulfill(status=200,content_type='text/css',body=baseline))
   page.goto('/');page.wait_for_selector('#msg',timeout=20000)
   page.evaluate('loadSession("copybrowser01")');page.wait_for_function('S.session && S.session.session_id==="copybrowser01"')
   for i in range(2):
    page.fill('#msg',f'Copy regression turn {i}');page.click('#btnSend')
    page.mouse.move(0,0)
    try:
     page.wait_for_function('(n) => !S.busy && S.messages.filter(m => m.role==="assistant" && String(m.content).includes("Lifecycle gate final answer.")).length>=n',arg=i+1,timeout=45000)
    except Exception:
     print('diagnostic',json.dumps({'root':str(root),'errors':errors,'text':page.locator('body').inner_text()[-2500:],'gateway':gateway.emitted_events}),flush=True);raise
    page.wait_for_timeout(1000)
    snap=page.evaluate('''() => Array.from(document.querySelectorAll('.assistant-turn')).map(t=>({html:t.outerHTML.slice(-9000),buttons:t.querySelectorAll('.msg-copy-btn').length,segments:Array.from(t.querySelectorAll('.assistant-segment')).map(s=>({raw:s.dataset.rawText,hidden:s.hidden}))}))''')
    (root/f'turn-{i}.json').write_text(json.dumps({'turns':snap,'errors':errors},indent=2))
    print('snapshot',i,json.dumps([{'buttons':s['buttons'],'segments':s['segments']} for s in snap]),flush=True)
    button=page.locator('.assistant-turn .msg-copy-btn:visible').last
    assert button.count(),'No visible message copy button after stream'
    hit=button.evaluate('''b=>{const r=b.getBoundingClientRect();return {pointer:getComputedStyle(b).pointerEvents,actionsOpacity:getComputedStyle(b.parentElement).opacity,hit:b.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}}''')
    assert hit['pointer']=='auto' and float(hit['actionsOpacity'])>0 and hit['hit'],('Copy must be directly available without hover/reload',hit)
    if mobile:button.tap(timeout=4000)
    else:button.click(timeout=4000)
    page.wait_for_timeout(100)
    copied=page.evaluate('navigator.clipboard.readText()')
    assert copied==FINAL_TEXT,(copied,FINAL_TEXT)
   page.reload();page.wait_for_function('S.session && !S.busy && S.messages.length>=6')
   page.mouse.move(0,0)
   button=page.locator('.assistant-turn .msg-copy-btn:visible').last
   if mobile:button.tap(timeout=4000)
   else:button.click(timeout=4000)
   assert page.evaluate('navigator.clipboard.readText()')==FINAL_TEXT
   assert not errors,errors
   (root/'receipt.json').write_text(json.dumps({'passed':True,'turns':2,'hard_reload_copy':True,'touch':mobile,'errors':errors,'css_sha256':hashlib.sha256((repo/'static/style.css').read_bytes()).hexdigest(),'model_boundary':'deterministic gateway; no external model','csp_bypassed':True,'service_workers':'blocked'},indent=2))
   browser.close()
  print(json.dumps({'passed':True,'turns':2,'artifact_dir':str(root),'errors':errors}),flush=True)
 finally:
  if proc:_terminate_process(proc)
  if log:log.close()
  gateway.close()

if __name__=='__main__':main()
