/* Trial-only diagnostics. No prompt, response, URL query, or credentials retained. */
(()=>{
'use strict';
const key='webui-timing-v1',page_id=String(Date.now())+'-'+Math.random().toString(16).slice(2,8);
let events=[],timer=0,request=0;
try{const v=JSON.parse(localStorage.getItem(key)||'[]');if(Array.isArray(v))events=v.slice(-2000);}catch(_){}
const fields=['session_id','stream_id','request_id','ms','count','chars','status','busy','visible','stage','offset_ms','duration_ms','round','max_ms'];
function save(){timer=0;try{localStorage.setItem(key,JSON.stringify(events));}catch(_){}}
function mark(event,data={}){try{const row={event:String(event).slice(0,64),page_id,wall_ms:Date.now(),t_ms:performance.now()};for(const k of fields){const v=data[k];if(typeof v==='number'&&Number.isFinite(v)||typeof v==='boolean')row[k]=v;else if(typeof v==='string')row[k]=v.slice(0,100);}events.push(row);if(events.length>2000)events.splice(0,events.length-2000);if(!timer)timer=setTimeout(save,1000);}catch(_){}}
function state(){return typeof S==='undefined'?{}:{session_id:S.session?.session_id,stream_id:S.activeStreamId,busy:!!S.busy,count:S.messages?.length,chars:document.getElementById('msgInner')?.textContent.length,visible:document.visibilityState==='visible'};}
window.webuiTiming={mark,exportData:()=>({schema:1,clock_note:'Compare t_ms only within the same page_id. Server duration_ms is monotonic; wall_ms is correlation only, not network latency.',events:events.slice()}),clear:()=>{events=[];save();}};
const nativeFetch=window.fetch;
window.fetch=async function(input,...args){let stage='';try{const p=new URL(typeof input==='string'?input:input.url,location.href).pathname;if(p.endsWith('/api/chat/start'))stage='chat_start';else if(p.endsWith('/api/session'))stage='session';}catch(_){}
 if(!stage)return nativeFetch.call(this,input,...args);const request_id=++request,t=performance.now(),owner=state();mark('request_start',{...owner,stage,request_id});try{const res=await nativeFetch.call(this,input,...args);mark('response_headers',{...owner,stage,request_id,ms:performance.now()-t,status:res.status});const original=res.json.bind(res);res.json=async()=>{const j=performance.now();try{const d=await original();mark('response_json_ready',{...owner,stream_id:d.stream_id||owner.stream_id,stage,request_id,ms:performance.now()-j});return d;}catch(e){mark('response_json_error',{...owner,stage,request_id});throw e;}};return res;}catch(e){mark('request_error',{...owner,stage,request_id,ms:performance.now()-t});throw e;}};
const NativeES=window.EventSource;
window.EventSource=class extends NativeES{constructor(url,...args){super(url,...args);let u;try{u=new URL(url,location.href);}catch(_){return;}if(!u.pathname.includes('chat/stream'))return;
const owner={session_id:u.searchParams.get('session_id')||state().session_id,stream_id:u.searchParams.get('stream_id')||state().stream_id};let first=true,last=0,count=0;mark('sse_attach',owner);
this.addEventListener('open',()=>mark('sse_open',owner));
this.addEventListener('token',()=>{last=performance.now();count++;if(first){first=false;mark('first_token',owner);}});
this.addEventListener('timing_probe',e=>{try{const d=JSON.parse(e.data);for(const s of d.stages||[])mark('server_stage',{...owner,stage:s.stage,duration_ms:s.duration_ms});mark('server_summary',{...owner,ms:d.writeback_ms,duration_ms:d.turn_ms});}catch(_){mark('probe_parse_error',owner);}});
this.addEventListener('done',e=>{const received=performance.now();mark('done_received',{...owner,chars:e.data.length,count,ms:last?received-last:undefined});queueMicrotask(()=>{mark('done_observer_microtask',{...state(),...owner,ms:performance.now()-received});requestAnimationFrame(()=>requestAnimationFrame(()=>{mark('post_done_paint_opportunity',{...state(),...owner,ms:performance.now()-received});save();}));});
const ready=()=>{const s=state();if(s.session_id!==owner.session_id){mark('readiness_observation_abandoned',owner);return;}if(!s.busy){mark('composer_ready',{...s,...owner,ms:performance.now()-received});save();}else if(performance.now()-received<60000)setTimeout(ready,50);else mark('composer_ready_timeout',owner);};setTimeout(ready,0);
for(const delay of [1000,5000,15000])setTimeout(()=>{mark('post_done_observation',{...state(),...owner,offset_ms:delay});save();},delay);});
this.addEventListener('stream_end',()=>{mark('stream_end',owner);save();});this.addEventListener('apperror',()=>mark('stream_error',owner));}}
function mount(){
 if(typeof setBusy==='function'){const original=setBusy;setBusy=function(...args){const before=state();try{return original.apply(this,args);}finally{mark('busy_change',{...state(),stream_id:before.stream_id});}};}
 if(typeof renderMessages==='function'){const original=renderMessages;renderMessages=function(...args){const t=performance.now();try{return original.apply(this,args);}finally{mark('render_messages',{...state(),ms:performance.now()-t});}};}
 try{new PerformanceObserver(list=>{for(const e of list.getEntries())mark('long_task',{...state(),ms:e.duration,offset_ms:e.startTime});}).observe({type:'longtask',buffered:false});}catch(_){}
 document.addEventListener('click',e=>{if(e.target.closest?.('#btnSend'))mark('send_click',state());},true);
 document.addEventListener('keydown',e=>{if(e.target.id==='msg'&&e.key==='Enter'&&!e.shiftKey)mark('send_enter',state());},true);
 document.addEventListener('visibilitychange',()=>mark('visibility',state()));
 const box=document.createElement('details');box.id='timingProbe';box.style.cssText='position:fixed;right:8px;bottom:4px;z-index:9999;background:var(--bg,#fff);color:var(--text,#222);border:1px solid #888;padding:4px;font-size:12px;max-width:260px';
 box.innerHTML='<summary>性能诊断 · 已开启</summary><p>仅记录时间与关联ID，最多2000点。</p><button type="button">导出诊断 JSON</button><button type="button">清空记录</button>';
 const buttons=box.querySelectorAll('button');buttons[0].onclick=()=>{save();const blob=new Blob([JSON.stringify(webuiTiming.exportData(),null,2)],{type:'application/json'}),a=document.createElement('a'),url=URL.createObjectURL(blob);a.href=url;a.download='webui-timing-'+Date.now()+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};buttons[1].onclick=()=>webuiTiming.clear();document.body.appendChild(box);mark('probe_ready',state());
}
document.addEventListener('DOMContentLoaded',mount);
})();
