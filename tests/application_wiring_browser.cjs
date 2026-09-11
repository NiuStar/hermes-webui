// DOM/transport wiring checks, not visual browser acceptance.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/application_tasks.js', 'utf8');
function setup() {
  const elements = new Map();
  const values = {applicationOperation:'create', applicationCandidateId:'b'.repeat(32),
    applicationTaskId:'', applicationApprovalId:'', applicationManifestSha:'',
    applicationPolicySha:'', applicationReference:'', applicationInterruptedTaskId:'',
    applicationDecision:'close_failed', applicationEvidenceSha:'', applicationOutput:'',
    applicationSubmitBtn:''};
  for (const [id,value] of Object.entries(values)) elements.set(id,{id,value,dataset:{},textContent:''});
  const storage = new Map();
  const context = {crypto:require('node:crypto').webcrypto, console,
    localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
    document:{getElementById:id=>elements.get(id)||null,querySelectorAll:()=>[]}};
  vm.createContext(context); vm.runInContext(source,context);
  return {context,elements,storage};
}
(async()=>{
  let checks = 0;
  {
    const {context,elements,storage} = setup(); const posts=[];
    context.api = async (path,opts) => {
      assert.equal(opts.retries,0);
      if (opts.method === 'POST') { posts.push(JSON.parse(opts.body)); throw new Error('lost response'); }
      const error = new Error('missing'); error.status=404; throw error;
    };
    await context.submitApplicationTask();
    const taskId=elements.get('applicationTaskId').value;
    assert.match(taskId,/^[0-9a-f]{32}$/);
    await context.submitApplicationTask();
    assert.equal(posts.length,2); assert.equal(posts[0].task_id,posts[1].task_id);
    assert.equal(JSON.parse(storage.get('hermes-application-request')).task_id,taskId);
    elements.get('applicationCandidateId').value='c'.repeat(32);
    await context.submitApplicationTask();
    assert.equal(posts.length,2);
    assert.equal(JSON.parse(elements.get('applicationOutput').textContent).issue.code,'CONFLICT');
    checks++;
  }
  {
    const {context,elements} = setup(); let seen;
    elements.get('applicationTaskId').value='a'.repeat(32);
    elements.get('applicationInterruptedTaskId').value='d'.repeat(32);
    context.api=async(path)=>{seen=path;return {evidence_sha:'e'.repeat(64),decision:'finalize_existing'};};
    await context.inspectApplicationRecovery();
    assert(seen.includes('/tasks/'+'d'.repeat(32)+'/recovery'));
    assert.equal(elements.get('applicationEvidenceSha').value,'e'.repeat(64));
    assert.equal(elements.get('applicationDecision').value,'finalize_existing');
    assert.equal(elements.get('applicationTaskId').value,'a'.repeat(32));
    checks++;
  }
  {
    const {context,elements} = setup(); let calls=0;
    context.api=async()=>{calls++;const error=new Error('busy');error.status=409;
      error.body=JSON.stringify({issue:{code:'BUSY',scope_type:'candidate',scope_id:'b'.repeat(32),retry_action:'check_status'}});throw error;};
    await context.submitApplicationTask();
    assert.equal(calls,1);
    assert.equal(JSON.parse(elements.get('applicationOutput').textContent).issue.code,'BUSY');
    assert.equal(elements.get('applicationSubmitBtn').disabled,false);
    checks++;
  }
  console.log(JSON.stringify({checks,verdict:'PASS',scope:'DOM_TRANSPORT_LOGIC_ONLY'}));
})().catch(error=>{console.error(error);process.exitCode=1;});
