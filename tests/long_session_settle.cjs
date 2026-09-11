// Run with node tests/long_session_settle.cjs from the repository root.
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const sessions=fs.readFileSync('static/sessions.js','utf8');
const refresh=sessions.slice(sessions.indexOf('async function refreshActiveSessionIfExternallyUpdated(reason)'),sessions.indexOf('\nfunction ensureActiveSessionExternalRefreshPoll()'));
async function probe(meta,remote){
 const reloads=[];const c={S:{session:{session_id:'long',message_count:3347,_metadataMessageCount:meta},messages:[{role:'assistant',content:'visible answer'}]},_loadingSessionId:null,_activeSessionExternalRefreshInFlight:false,window:{},document:{hidden:false},_isExternalSession:()=>true,_drainSessionUpdatedPendingCount:()=>{},api:async()=>({session:{message_count:remote}}),loadSession:async(s,o)=>reloads.push(o)};
 vm.createContext(c);vm.runInContext(refresh,c);const result=await c.refreshActiveSessionIfExternallyUpdated('idle-reconcile');return {result,reloads};
}
(async()=>{
 const stable=await probe(3345,3345);assert.equal(stable.result,'unchanged','merged display count must not be compared to storage metadata count');
 for(const remote of [3344,3346]){const p=await probe(3345,remote);assert.equal(p.result,'reloaded');assert.equal(p.reloads[0].keepStaleUntilLoaded,true,'idle reconcile must keep the visible answer until replacement arrives');}
 console.log('PASS metadata coordinate, growth/shrink and non-destructive refresh');
 const begin=sessions.indexOf('  const _previousMetadataMessageCount=');
 const legacy=sessions.indexOf('  S.session=data.session;\n',sessions.indexOf('async function loadSession('));
 const stop=sessions.indexOf("  if(typeof _adoptRegenerationRevision",begin>=0?begin:legacy);
 const c0={S:{session:{_metadataMessageCount:3345}},data:{session:{message_count:3347}},_keepStaleUntilLoaded:true};
 vm.createContext(c0);vm.runInContext(sessions.slice(begin>=0?begin:legacy,stop),c0);
 assert.equal(c0.S.session._metadataMessageCount,3345,'metadata arrival must not acknowledge messages before successful load');
 const src=fs.readFileSync('static/messages.js','utf8');
 const adoption=src.indexOf('          d.session=_settledSessionForCurrentWindow(d.session);');
 const start=adoption>=0?adoption:src.indexOf('          S.session=d.session;');
 const end=src.indexOf('          if(typeof _hydrateTodosFromSession',start);
 assert.ok(start>=0&&end>start);
 const helperStart=src.indexOf('function _settledSessionForCurrentWindow(');
 const helper=helperStart<0?'':src.slice(helperStart,src.indexOf('\nfunction ',helperStart+10));
 const all=Array.from({length:3347},(_,i)=>({role:'assistant',content:String(i)}));
 for(const offset of [3317,3217,0]){
  const c={S:{session:{session_id:'long',_metadataMessageCount:3345},messages:all.slice(offset)},d:{session:{session_id:'long',message_count:3347,messages:all}},_messagesTruncated:offset>0,_oldestIdx:offset,_carryForwardEphemeralTurnFields:(_,n)=>n,_filterRecoveryControlMessages:m=>m};
  vm.createContext(c);vm.runInContext(helper+src.slice(start,end),c);
  assert.equal(c.S.messages.length,3347-offset,'done must retain the loaded page, not expand all history');
  assert.equal(c._oldestIdx,offset);assert.equal(c.S.session._metadataMessageCount,3345);
  assert.equal(c.S.messages.at(-1).content,'3346');
 }
 console.log('PASS terminal page width, offset, metadata baseline and final answer');
 for(const [keep,rows,preserve] of [[true,[{role:'assistant'}],true],[true,[],false],[false,[{role:'assistant'}],false]]){
  const dom={innerHTML:'retained answer'};
  const c={S:{messages:rows},_keepStaleUntilLoaded:keep,$:()=>dom};
  const a=sessions.indexOf("      const _msgInner = $('msgInner');",sessions.indexOf('// Phase 2b:'));
  const b=sessions.indexOf("      if (typeof showToast",a);
  vm.createContext(c);vm.runInContext(sessions.slice(a,b),c);
  assert.equal(dom.innerHTML==='retained answer',preserve,'failed same-session sync must retain DOM; cross-session failure must not');
 }
 console.log('PASS failed refresh DOM preservation and cross-session boundary');
})().catch(e=>{console.error(e);process.exitCode=1});
