const vm=require('vm'),fs=require('fs'),assert=require('assert/strict');
const path='static/timing_probe.js';assert(fs.existsSync(path),'timing probe must exist');
let saved={};const c={performance:{now:()=>10},Date,URL,console,setTimeout:()=>0,requestAnimationFrame:()=>0,localStorage:{getItem:k=>saved[k]||null,setItem:(k,v)=>saved[k]=v},location:{href:'http://example/'},document:{addEventListener:()=>{},getElementById:()=>null,visibilityState:'visible'},EventSource:class{},fetch:async()=>({}),window:null};c.window=c;vm.createContext(c);vm.runInContext(fs.readFileSync(path,'utf8'),c);
c.webuiTiming.mark('test',{session_id:'abc',stream_id:'def',ms:2,content:'SECRET',password:'SECRET'});
let rows=c.webuiTiming.exportData().events;assert.equal(rows.at(-1).content,undefined);assert.equal(rows.at(-1).password,undefined);assert.equal(rows.at(-1).ms,2);
for(let i=0;i<2200;i++)c.webuiTiming.mark('test',{ms:i});assert(c.webuiTiming.exportData().events.length<=2000);
console.log('PASS bounded timing records and field allowlist');
