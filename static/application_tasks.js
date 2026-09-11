// Application runtime v2. No automatic write retries; every write retains its ID.
let _applicationLastRequestId = localStorage.getItem('hermes-application-request-id') || '';
let _applicationSubmitting = false;
let _applicationSelectedTaskId = '';

function _applicationId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
}
function _applicationValue(id) {
  const element = document.getElementById(id);
  return element ? element.value.trim() : '';
}
function _applicationSet(id, value) {
  const element = document.getElementById(id);
  if (element) element.value = value || '';
}
function _applicationRender(payload) {
  const output = document.getElementById('applicationOutput');
  if (output) {
    output.textContent = JSON.stringify(payload, null, 2);
    output.dataset.issue = payload && payload.issue ? payload.issue.code : '';
  }
}
function _applicationError(error) {
  try {
    const payload = JSON.parse(error.body);
    if (payload && payload.issue) return payload;
  } catch (_) { /* Network errors have no HTTP body. */ }
  return {request_id: _applicationLastRequestId || null,
    issue: {code: error.status === 401 ? 'UNAUTHENTICATED' : 'REQUEST_FAILED',
      retry_action: 'check_status', message: error.message}};
}
async function _applicationApi(path, options = {}) {
  const payload = await api(path, {...options, retries: 0, redirect401: false});
  if (!payload || typeof payload !== 'object') {
    const error = new Error('请重新登录后查询原任务；不要直接新建重试。');
    error.status = 401;
    throw error;
  }
  return payload;
}
function newApplicationRequest() {
  if (_applicationSubmitting) return;
  _applicationLastRequestId = _applicationId();
  _applicationSet('applicationTaskId', _applicationLastRequestId);
  localStorage.setItem('hermes-application-request-id', _applicationLastRequestId);
  localStorage.removeItem('hermes-application-request');
}
function restoreApplicationRequest() {
  _applicationSet('applicationTaskId', _applicationLastRequestId);
  try {
    const request = JSON.parse(localStorage.getItem('hermes-application-request'));
    if (!request || request.task_id !== _applicationLastRequestId) return;
    _applicationSet('applicationOperation', request.operation);
    const fields = {candidate_id: 'applicationCandidateId', approval_id: 'applicationApprovalId',
      manifest_sha: 'applicationManifestSha', policy_sha: 'applicationPolicySha', reference: 'applicationReference',
      interrupted_task_id: 'applicationInterruptedTaskId', decision: 'applicationDecision',
      expected_evidence_sha: 'applicationEvidenceSha'};
    Object.entries(fields).forEach(([key, id]) => _applicationSet(id, request.parameters[key]));
  } catch (_) { /* Malformed local drafts never authorize a write. */ }
  syncApplicationFields();
}
async function loadApplicationCapabilities() {
  try {
    const data = await _applicationApi('/api/application/tasks/capabilities');
    const element = document.getElementById('applicationCapabilities');
    if (element) element.textContent = `服务身份：${data.principal}；权限：${(data.permissions || []).join('、')}；发布不激活`;
    if (!_applicationValue('applicationPolicySha')) _applicationSet('applicationPolicySha', data.policy_sha);
  } catch (error) {
    _applicationRender(_applicationError(error));
  }
}
async function loadApplicationTasks() {
  const list = document.getElementById('applicationTaskList');
  if (!list) return;
  try {
    const data = await _applicationApi('/api/application/tasks?limit=20');
    list.replaceChildren(...(data.tasks || []).map(task => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'application-task-row';
      button.textContent = `${task.operation}  ${task.candidate_id}  ${task.state}`;
      button.onclick = () => {
        _applicationSet('applicationCandidateId', task.candidate_id);
        _applicationSet('applicationInterruptedTaskId', task.id);
        inspectApplicationTask(task.id);
      };
      return button;
    }));
    if (!(data.tasks || []).length) list.textContent = '暂无应用任务';
  } catch (error) {
    list.textContent = error.message;
    _applicationRender(_applicationError(error));
  }
}
async function inspectApplicationTask(taskId) {
  const id = taskId || _applicationValue('applicationTaskId') || _applicationLastRequestId;
  if (!id) return;
  _applicationSelectedTaskId = id;
  try {
    _applicationRender(await _applicationApi(`/api/application/tasks/${encodeURIComponent(id)}`));
  } catch (error) {
    _applicationRender(_applicationError(error));
  }
}
async function inspectApplicationRecovery() {
  const id = _applicationValue('applicationInterruptedTaskId') || _applicationSelectedTaskId ||
    _applicationValue('applicationTaskId') || _applicationLastRequestId;
  const candidate = _applicationValue('applicationCandidateId');
  if (!id || !candidate) return;
  try {
    const data = await _applicationApi(`/api/application/tasks/${encodeURIComponent(id)}/recovery?candidate_id=${encodeURIComponent(candidate)}`);
    _applicationSet('applicationInterruptedTaskId', id);
    _applicationSet('applicationEvidenceSha', data.evidence_sha);
    if (['close_failed', 'finalize_existing', 'resume_publish'].includes(data.decision)) {
      _applicationSet('applicationDecision', data.decision);
    }
    _applicationRender(data); // Inspection never submits recovery automatically.
  } catch (error) {
    _applicationRender(_applicationError(error));
  }
}
function _applicationRequest() {
  const operation = _applicationValue('applicationOperation');
  const taskId = _applicationValue('applicationTaskId') || _applicationLastRequestId || _applicationId();
  const approvalId = _applicationValue('applicationApprovalId');
  const parameters = {candidate_id: _applicationValue('applicationCandidateId')};
  if (operation === 'approve') Object.assign(parameters, {
    approval_id: approvalId, manifest_sha: _applicationValue('applicationManifestSha'),
    policy_sha: _applicationValue('applicationPolicySha'), reference: _applicationValue('applicationReference')});
  else if (operation === 'publish') parameters.approval_id = approvalId;
  else if (operation === 'recover') Object.assign(parameters, {
    interrupted_task_id: _applicationValue('applicationInterruptedTaskId'),
    decision: _applicationValue('applicationDecision'), expected_evidence_sha: _applicationValue('applicationEvidenceSha'),
    approval_id: _applicationValue('applicationDecision') === 'close_failed' ? null : approvalId || null});
  return {mode: 'application_runtime_v2', task_id: taskId, operation, parameters};
}
async function submitApplicationTask() {
  if (_applicationSubmitting) return;
  _applicationSubmitting = true;
  const button = document.getElementById('applicationSubmitBtn');
  if (button) button.disabled = true;
  try {
    const request = _applicationRequest();
    const raw = JSON.stringify(request);
    const saved = localStorage.getItem('hermes-application-request');
    if (saved) {
      const old = JSON.parse(saved);
      if (old.task_id === request.task_id && saved !== raw) {
        _applicationRender({issue: {code: 'CONFLICT', message: '同一任务 ID 的请求不可修改；查询原任务，或明确新建请求。'}});
        return;
      }
    }
    // Persist the complete request before network I/O, including generated ID.
    _applicationLastRequestId = request.task_id;
    _applicationSet('applicationTaskId', request.task_id);
    localStorage.setItem('hermes-application-request-id', request.task_id);
    localStorage.setItem('hermes-application-request', raw);
    try {
      const existing = await _applicationApi(`/api/application/tasks/${encodeURIComponent(request.task_id)}`);
      _applicationRender(existing);
      return; // Query/recovery, not another write, after an uncertain outcome.
    } catch (error) {
      if (error.status !== 404) throw error;
    }
    const path = request.operation === 'recover'
      ? `/api/application/tasks/${encodeURIComponent(request.parameters.interrupted_task_id)}/recovery`
      : '/api/application/tasks';
    _applicationRender(await _applicationApi(path, {method: 'POST', body: raw}));
    await loadApplicationTasks();
  } catch (error) {
    _applicationRender(_applicationError(error));
  } finally {
    _applicationSubmitting = false;
    if (button) button.disabled = false;
  }
}
function syncApplicationFields() {
  const operation = _applicationValue('applicationOperation');
  document.querySelectorAll('[data-application-operations]').forEach(row => {
    row.hidden = !row.dataset.applicationOperations.split(' ').includes(operation);
  });
}
if (typeof window !== 'undefined') window.addEventListener('DOMContentLoaded', restoreApplicationRequest);
