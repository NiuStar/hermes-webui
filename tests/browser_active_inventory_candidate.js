// Isolated candidate DOM/layout test; no production authentication or API calls.
const fs = require('fs');
const assert = require('assert');
const {chromium} = require('playwright');
const dir = process.env.CANDIDATE_DIR;
const html = fs.readFileSync(`${dir}/static/index.html`, 'utf8');
const css = fs.readFileSync(`${dir}/static/style.css`, 'utf8');
const source = fs.readFileSync(`${dir}/static/sessions.js`, 'utf8');
const start = source.indexOf('function _activeSessionInventoryView(');
const end = source.indexOf('\nfunction _startActiveSessionInventoryPoll(', start);
assert(start >= 0 && end > start);
const js = source.slice(start, end);
(async () => {
  const browser = await chromium.launch({headless:true,args:['--no-sandbox']});
  try {
    for (const width of [320, 375, 640, 1280]) {
      const page = await browser.newPage({viewport:{width,height:720}});
      const errors=[]; page.on('pageerror',e=>errors.push(e.message));
      const header=html.slice(html.indexOf('<header class="app-titlebar"'),html.indexOf('</header>')+9);
      await page.setContent(`<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><style>${css}</style></head><body>${header}<div class="layout"><aside class="sidebar"><div class="panel-view active" id="panelChat"><div id="activeSessionInventory" class="active-session-inventory" hidden></div></div></aside></div></body></html>`);
      await page.locator('#titlebarProfileBtn').evaluate(el=>{el.style.display='inline-flex';document.getElementById('titlebarProfileLabel').textContent='long-profile-name'});
      await page.locator('#appTitlebarTitle').evaluate(el=>el.textContent='A very long conversation title with unusual long words and wrapping');
      await page.evaluate(js => {
        window.S={activeProfile:'default',session:{session_id:'here'}};
        window.$=id=>document.getElementById(id);
        window.t=k=>({active_sessions_unknown:'状态未知',active_sessions_count:'运行中任务：{0}',active_sessions_titlebar:'运行中 · {0}',active_background_running:'后台运行中',active_background_done:'已结束',active_background_unknown:'待确认',active_process_running:'进程运行中',active_delegation_running:'子代理运行中',active_auxiliary_ended:'已停止',active_sessions_ended:'已停止 {0}'}[k]||k);
        window.switchPanel=async()=>{window.panelSwitched=true;return 'chat'};
        window._isSidebarCollapsed=()=>false;
        window.toggleMobileSidebar=()=>document.querySelector('.sidebar').classList.add('mobile-open');
        window._openSidebarSession=async target=>{window.opened=target};
        window.showToast=()=>{};
        (0,eval)(js);
      },js);
      await page.evaluate(() => _renderActiveSessionInventory({known:true,profile:'default',count:2,sessions:[{session_id:'here',title:'Current'},{session_id:'else',title:'Other'}],background:[],auxiliary:[],ended:[]}));
      const button=page.locator('#backgroundRunCount');
      const box=await button.boundingBox();
      const layout=await page.evaluate(()=>{const b=document.getElementById('backgroundRunCount'); const r=b.getBoundingClientRect(); const neighbors=[...document.querySelector('header').children].map(e=>({name:e.className,right:e.getBoundingClientRect().right,left:e.getBoundingClientRect().left,width:e.getBoundingClientRect().width})); return {text:b.innerText,scrollWidth:document.documentElement.scrollWidth,width:innerWidth,visible:getComputedStyle(b).display!=='none'&&r.left>=0&&r.right<=innerWidth,compact:getComputedStyle(b.querySelector('.active-task-count-compact')).display,neighbors};});
      assert(box&&layout.visible&&layout.scrollWidth<=width,`${width}: button clipped or document overflow: ${JSON.stringify(layout)}`);
      if(width<=375)assert(layout.neighbors.find(n=>n.name==='app-titlebar-inner').width>=40,`${width}: title collapsed behind controls`);
      assert(layout.text.includes('2'),`${width}: count not visible: ${JSON.stringify(layout)}`);
      await button.click();
      assert(await page.evaluate(()=>window.panelSwitched),`${width}: chat panel not selected`);
      if(width<=640)assert(await page.locator('.sidebar').evaluate(el=>el.classList.contains('mobile-open')),`${width}: mobile drawer not opened`);
      assert((await page.locator('#activeSessionInventory button').count())>=2,`${width}: detail not rendered`);
      await page.evaluate(()=>_renderActiveSessionInventory({known:false,profile:'default'}));
      assert((await button.innerText()).includes('?'),`${width}: unknown became zero`);
      assert.equal(await page.locator('#activeSessionInventory button').count(),0,`${width}: stale entries stayed visible`);
      // Exercise the real polling function with an authenticated API-shaped result,
      // a rejected request, and an in-flight profile switch (no old count leak).
      await page.evaluate(async () => {
        S.activeProfile='default';
        window.api=async()=>({count:2,active_profile:'default',sessions:[{session_id:'here',title:'Current'},{session_id:'else',title:'Other'}],background_tasks:[],auxiliary_tasks:[]});
        await _refreshActiveSessionInventory();
        if(!document.getElementById('backgroundRunCount').innerText.includes('2'))throw Error('successful poll did not render 2');
        window.api=async()=>{throw Error('HTTP 503')};
        await _refreshActiveSessionInventory();
        if(!document.getElementById('backgroundRunCount').innerText.includes('?'))throw Error('503 kept old count');
        window.api=async()=>{S.activeProfile='other';return {count:2,active_profile:'default',sessions:[],background_tasks:[],auxiliary_tasks:[]}};
        await _refreshActiveSessionInventory();
        if(!document.getElementById('backgroundRunCount').innerText.includes('?'))throw Error('profile switch leaked old count');
      });
      assert.deepStrictEqual(errors,[],`${width}: page errors`);
      console.log(JSON.stringify({width,layout,interaction:true,unknown:true}));
      await page.close();
    }
  } finally {await browser.close();}
})().catch(e=>{console.error(e.stack);process.exitCode=1});
