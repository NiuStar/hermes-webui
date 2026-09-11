import json
import os
import pwd
import shutil
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from api.application_operation_issue import ApplicationIssue
from api.application_protocol import MODE, Principal
from api.application_runtime_config import load_config
from api.application_resource_owner import owner_alive
from api.application_task_service import ApplicationService
from api.application_protocol import digest, parse_request, canonical
from api.application_artifact_files import read_record


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "application_task_entry.py"
APPLICATION_JS = ROOT / "static" / "application_tasks.js"
INDEX_HTML = ROOT / "static" / "index.html"
STYLE_CSS = ROOT / "static" / "style.css"


def _id(number):
    return f"{number:032x}"


def _request(task_id, operation, parameters):
    return {
        "mode": MODE,
        "task_id": task_id,
        "operation": operation,
        "parameters": parameters,
    }


def _config_data(root, principals, *, include_quota=False):
    data = {
        "runtime_format": MODE,
        "ledger_path": str(root / "ledger.sqlite"),
        "artifact_root": str(root / "artifacts"),
        "artifact_policy": {"publication": "empty-unactivated"},
        "creator_commit": "test-commit",
        "principals": principals,
    }
    if include_quota:
        data["quota"] = {"enabled": False}
    return data


def _write_config(path, data, mode=0o600):
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(mode)


@pytest.fixture
def runtime(tmp_path):
    config_path = tmp_path / "application.json"
    principals = {
        "creator": {"permissions": ["create", "recover"], "task_read_all": False},
        "approver": {"permissions": ["approve"], "task_read_all": True},
        "publisher": {"permissions": ["publish", "recover"], "task_read_all": True},
        "observer": {"permissions": [], "task_read_all": False},
    }
    _write_config(config_path, _config_data(tmp_path, principals))
    config = load_config(config_path)
    service = ApplicationService(config)
    service.initialize()
    return service, config


def _complete_round(service, config, round_number):
    candidate_id = _id(100 + round_number)
    create_id = _id(200 + round_number * 3)
    approve_id = _id(201 + round_number * 3)
    publish_id = _id(202 + round_number * 3)
    approval_id = _id(300 + round_number)

    create_request = _request(create_id, "create", {"candidate_id": candidate_id})
    created = service.submit(create_request, config.principal("creator"))
    assert created["task_state"] == "SUCCEEDED"
    assert created["result"]["business_status"] == "VERIFIED"

    approve_request = _request(approve_id, "approve", {
        "candidate_id": candidate_id,
        "approval_id": approval_id,
        "manifest_sha": created["result"]["manifest_sha"],
        "policy_sha": config.artifact_policy_sha,
        "reference": f"review-{round_number}",
    })
    approved = service.submit(approve_request, config.principal("approver"))
    assert approved["result"]["business_status"] == "APPROVAL_DURABLE"

    publish_request = _request(publish_id, "publish", {
        "candidate_id": candidate_id,
        "approval_id": approval_id,
    })
    published = service.submit(publish_request, config.principal("publisher"))
    assert published["result"]["business_status"] == "PUBLISHED_UNACTIVATED"
    assert published["result"]["activated"] is False
    assert not (config.artifact_root / "candidates" / candidate_id).exists()
    assert (config.artifact_root / "published" / candidate_id / "display.sqlite").is_file()

    with sqlite3.connect(config.ledger_path) as db:
        assert db.execute(
            "SELECT state FROM candidate_heads WHERE candidate_id=?", (candidate_id,)
        ).fetchone() == ("PUBLISHED_UNACTIVATED",)
        assert db.execute(
            "SELECT COUNT(*) FROM events WHERE candidate_id=?", (candidate_id,)
        ).fetchone() == (5,)

    return create_request, created, published


def test_real_sqlite_create_approve_publish_three_rounds_and_replay(runtime):
    service, config = runtime
    first_request = first_result = None
    for round_number in range(3):
        create_request, created, published = _complete_round(service, config, round_number)
        assert published["result"]["completion_record_sha"]
        if round_number == 0:
            first_request, first_result = create_request, created

    replay = service.submit(first_request, config.principal("creator"))
    assert replay == first_result
    assert len(service.list(config.principal("publisher"))["tasks"]) == 9

    conflicting = dict(first_request)
    conflicting["parameters"] = {"candidate_id": _id(999)}
    with pytest.raises(ApplicationIssue) as caught:
        service.submit(conflicting, config.principal("creator"))
    assert caught.value.code == "CONFLICT"


def test_publish_audit_matches_persisted_result_and_intent_precedes_completion(runtime):
    service, config = runtime
    _, _, published = _complete_round(service, config, 20)
    task_id = published["result"]["task_id"]
    audit = read_record(config.artifact_root / "audits" / f"{task_id}.json")
    assert audit["result_sha"] == digest(published["result"])

    with sqlite3.connect(config.ledger_path) as db:
        records = [json.loads(row[0]) for row in db.execute(
            "SELECT record FROM events WHERE candidate_id=? ORDER BY seq", (_id(120),)
        )]
    assert [record["state"] for record in records] == [
        "BUILDING", "VERIFIED", "APPROVED", "PUBLISH_INTENT", "PUBLISHED_UNACTIVATED"
    ]
    assert published["result"]["completion_record_sha"] == records[-1]["record_sha"]


def test_publish_persists_intent_before_rename(runtime):
    service, config = runtime
    candidate_id = _id(730)
    created = service.submit(_request(_id(731), "create", {"candidate_id": candidate_id}),
                             config.principal("creator"))
    service.submit(_request(_id(732), "approve", {
        "candidate_id": candidate_id, "approval_id": _id(733),
        "manifest_sha": created["result"]["manifest_sha"],
        "policy_sha": config.artifact_policy_sha, "reference": "intent-order",
    }), config.principal("approver"))

    def assert_intent_then_rename(source, target):
        with sqlite3.connect(config.ledger_path) as db:
            head = db.execute("SELECT state FROM candidate_heads WHERE candidate_id=?",
                              (candidate_id,)).fetchone()
        assert head == ("PUBLISH_INTENT",)
        os.rename(source, target)

    with patch("api.application_artifact_files._rename_no_replace", assert_intent_then_rename):
        service.submit(_request(_id(734), "publish", {
            "candidate_id": candidate_id, "approval_id": _id(733),
        }), config.principal("publisher"))


def test_same_recovery_attempt_replays_after_publish_effect_without_repeating_it(runtime):
    service, config = runtime
    candidate_id, approval_id = _id(740), _id(743)
    created = service.submit(_request(_id(741), "create", {"candidate_id": candidate_id}),
                             config.principal("creator"))
    service.submit(_request(_id(742), "approve", {
        "candidate_id": candidate_id, "approval_id": approval_id,
        "manifest_sha": created["result"]["manifest_sha"],
        "policy_sha": config.artifact_policy_sha, "reference": "replay",
    }), config.principal("approver"))
    original_request = _request(_id(744), "publish", {
        "candidate_id": candidate_id, "approval_id": approval_id,
    })
    original = parse_request(original_request)
    publisher = config.principal("publisher")
    service._admit_task(original, publisher, "candidate:" + candidate_id)
    service._start_task(original, "candidate:" + candidate_id)
    service._intent(original.task_id, "publish_rename", {
        "operation": "publish", "parameters": dict(original.parameters),
        "policy_sha": config.artifact_policy_sha,
    }, source=str(service.files.candidates / candidate_id),
       target=str(service.files.published / candidate_id))
    service._publish_intent(original, {"approval_id": approval_id})
    with sqlite3.connect(config.ledger_path) as db:
        db.execute("UPDATE resource_owners SET boot_id=? WHERE resource_key=?",
                   ("00000000-0000-0000-0000-000000000001", "candidate:" + candidate_id))

    inspection = service.inspect_recovery(original.task_id, candidate_id, publisher)
    recovery_raw = _request(_id(745), "recover", {
        "candidate_id": candidate_id, "interrupted_task_id": original.task_id,
        "decision": "resume_publish", "expected_evidence_sha": inspection["evidence_sha"],
        "approval_id": approval_id,
    })
    recovery = parse_request(recovery_raw)
    observed = service.inspect_recovery(original.task_id, candidate_id, publisher)
    service._admit_recovery(recovery, publisher, service.ledger.lookup(original.task_id, publisher), observed)
    service._intent(recovery.task_id, "recover_publish_rename", {
        "decision": "resume_publish", "evidence_sha": inspection["evidence_sha"],
        "original_task_id": original.task_id,
    })
    service.files.publish(candidate_id, approval_id)
    # Simulate interrupted executor; a still-live owner must never be taken over.
    with sqlite3.connect(config.ledger_path) as db:
        db.execute("UPDATE resource_owners SET boot_id=? WHERE resource_key=?",
                   ("00000000-0000-0000-0000-000000000001", "candidate:" + candidate_id))

    with patch.object(service.files, "publish", side_effect=AssertionError("effect repeated")):
        replayed = service.submit(recovery_raw, publisher)
    assert replayed["task_state"] == "SUCCEEDED"
    assert replayed["result"]["business_status"] == "PUBLISHED_UNACTIVATED"
    assert service.submit(recovery_raw, publisher) == replayed


def test_permissions_and_task_visibility(runtime):
    service, config = runtime
    request, created, _ = _complete_round(service, config, 7)

    with pytest.raises(ApplicationIssue) as denied:
        service.submit(_request(_id(900), "create", {"candidate_id": _id(901)}), config.principal("observer"))
    assert denied.value.code == "PERMISSION_DENIED"

    with pytest.raises(ApplicationIssue) as hidden:
        service.get(request["task_id"], config.principal("observer"))
    assert hidden.value.code == "NOT_FOUND"
    assert service.list(config.principal("observer"))["tasks"] == []
    assert service.get(request["task_id"], config.principal("approver")) == created


def test_recovery_closes_interrupted_create(runtime):
    service, config = runtime
    principal = config.principal("creator")
    candidate_id = _id(700)
    interrupted_id = _id(701)
    request = _request(interrupted_id, "create", {"candidate_id": candidate_id})

    service._admit_task(service_request := __import__(
        "api.application_protocol", fromlist=["parse_request"]
    ).parse_request(request), principal, "candidate:" + candidate_id)
    service._start_task(service_request, "candidate:" + candidate_id)
    # No complete artifact: close_failed must remain distinct from successful recovery.
    with sqlite3.connect(config.ledger_path) as db:
        db.execute(
            "UPDATE resource_owners SET boot_id=? WHERE resource_key=?",
            ("00000000-0000-0000-0000-000000000001", "candidate:" + candidate_id),
        )

    inspection = service.inspect_recovery(interrupted_id, candidate_id, principal)
    assert inspection["decision"] == "close_failed"
    recovery_request = _request(_id(702), "recover", {
        "candidate_id": candidate_id,
        "interrupted_task_id": interrupted_id,
        "decision": "close_failed",
        "expected_evidence_sha": inspection["evidence_sha"],
        "approval_id": None,
    })
    recovered = service.submit(recovery_request, principal)
    assert recovered["task_state"] == "SUCCEEDED"
    assert recovered["result"]["business_status"] == "RECOVERED_FAILED"
    assert service.get(interrupted_id, principal)["task_state"] == "FAILED"
    assert service.inspect_recovery(interrupted_id, candidate_id, principal)["decision"] == "already_terminal"


def test_owner_alive_parses_parenthesized_comm_and_treats_unreadable_as_alive():
    owner = {"owner_state": "ACTIVE", "boot_id": "boot", "pid": 123, "start_ticks": 987}
    stat_text = "123 (hostile name ) with spaces) S " + " ".join(["0"] * 18 + ["987", "0"])

    def readable(path, *args, **kwargs):
        value = str(path)
        if value.endswith("boot_id"):
            return "boot\n"
        if value.endswith("/123/stat"):
            return stat_text
        raise AssertionError(value)

    with patch.object(Path, "read_text", readable):
        assert owner_alive(owner) is True
    with patch.object(Path, "read_text", side_effect=PermissionError):
        assert owner_alive(owner) is True
    with patch.object(Path, "read_text", return_value="malformed"):
        assert owner_alive(owner) is True


def test_missing_quota_defaults_disabled(tmp_path):
    path = tmp_path / "config.json"
    _write_config(path, _config_data(tmp_path, {
        "creator": {"permissions": ["create"], "task_read_all": False},
    }))
    config = load_config(path)
    assert ApplicationService(config).initialize()["quota_enabled"] is False


def test_cli_runs_as_nobody_and_rejects_identity_spoofing(tmp_path):
    if pwd.getpwuid(os.geteuid()).pw_name != "nobody":
        pytest.skip("requires the test process to run as nobody")
    work = tmp_path / "nobody-runtime"
    work.mkdir(mode=0o700)
    config_path = work / "config.json"
    principals = {
        "nobody": {"permissions": ["create", "recover"], "task_read_all": False},
        "spoofed": {"permissions": ["create"], "task_read_all": True},
    }
    _write_config(config_path, _config_data(work, principals), mode=0o600)
    env = dict(os.environ, PYTHONPATH=f".:/opt/application-wal-guard-d8881/deps")

    def run(*args, input_text=None, extra_env=None):
        command_env = dict(env)
        if extra_env:
            command_env.update(extra_env)
        return subprocess.run(
            ["python3", str(CLI), "--config", str(config_path), *args],
            cwd=ROOT,
            env=command_env,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=60,
        )

    initialized = run("init")
    assert initialized.returncode == 0, initialized.stderr
    assert json.loads(initialized.stdout)["status"] == "INITIALIZED"

    spoofed = run("--principal", "spoofed", "list")
    assert spoofed.returncode == 3
    assert json.loads(spoofed.stdout)["issue"]["code"] == "PERMISSION_DENIED"


def test_cli_rejects_root_execution(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("requires root")
    config_path = tmp_path / "config.json"
    _write_config(config_path, _config_data(tmp_path, {
        "root": {"permissions": ["create"], "task_read_all": True},
    }))

    root_attempt = subprocess.run(
        ["python3", str(CLI), "--config", str(config_path), "--principal", "root", "list"],
        cwd=ROOT,
        env=os.environ,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert root_attempt.returncode == 3
    assert json.loads(root_attempt.stdout)["issue"]["code"] == "PERMISSION_DENIED"


def test_application_panel_dom_and_styles_are_present():
    html = INDEX_HTML.read_text(encoding="utf-8")
    css = STYLE_CSS.read_text(encoding="utf-8")
    required_ids = {
        "panelApplications", "mainApplications", "applicationTaskList", "applicationOutput",
        "applicationOperation", "applicationCandidateId", "applicationTaskId", "applicationApprovalId",
        "applicationManifestSha", "applicationPolicySha", "applicationReference",
        "applicationInterruptedTaskId", "applicationDecision", "applicationEvidenceSha",
        "applicationInspectBtn",
    }
    for element_id in required_ids:
        assert f'id="{element_id}"' in html
    assert ".application-runtime" in css
    assert "@media" in css


def test_preflight_posts_only_after_404():
    if shutil.which("node") is None:
        pytest.skip("node is required for browser logic test")
    source = APPLICATION_JS.read_text(encoding="utf-8")
    harness = r"""
const vm = require('vm');
const source = process.env.APPLICATION_SOURCE;
const elements = new Map();
function element(id, value='') { return {id, value, hidden:false, dataset:{}, textContent:'', replaceChildren(){}}; }
for (const [id,value] of Object.entries({
  applicationOperation:'create', applicationCandidateId:'00000000000000000000000000000001',
  applicationTaskId:'00000000000000000000000000000002', applicationApprovalId:'',
  applicationManifestSha:'', applicationPolicySha:'', applicationReference:'',
  applicationInterruptedTaskId:'', applicationDecision:'close_failed', applicationEvidenceSha:'',
  applicationOutput:''
})) elements.set(id, element(id,value));
const context = {
  console, crypto:require('crypto').webcrypto,
  localStorage:{getItem(){return ''},setItem(){}},
  document:{getElementById(id){return elements.get(id)||null},querySelectorAll(){return []}},
};
vm.createContext(context); vm.runInContext(source, context);
async function scenario(status) {
  const calls=[];
  context.api=async (path,opts) => {
    calls.push({path,method:opts&&opts.method});
    if (!opts || !opts.method) { const error=new Error('preflight'); error.status=status; throw error; }
    return {accepted:true};
  };
  await context.submitApplicationTask();
  return calls;
}
(async()=>console.log(JSON.stringify({notFound:await scenario(404), server:await scenario(500), network:await scenario(undefined)})))();
"""
    result = subprocess.run(
        ["node", "-e", harness],
        cwd=ROOT,
        env=dict(os.environ, APPLICATION_SOURCE=source),
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    calls = json.loads(result.stdout)
    assert [call.get("method") for call in calls["notFound"]] == [None, "POST"]
    assert len(calls["server"]) == 1
    assert len(calls["network"]) == 1


def test_real_double_sigkill_recovery(runtime):
    import signal
    import sys
    service, config = runtime
    cid, aid = _id(810), _id(811)
    created = service.submit(_request(_id(812), "create", {"candidate_id": cid}), config.principal("creator"))
    service.submit(_request(_id(813), "approve", {
        "candidate_id": cid, "approval_id": aid,
        "manifest_sha": created["result"]["manifest_sha"],
        "policy_sha": config.artifact_policy_sha, "reference": "double kill"
    }), config.principal("approver"))
    config_path = config.ledger_path.parent / "application.json"
    victim = _request(_id(814), "publish", {"candidate_id": cid, "approval_id": aid})
    script = """
import json,os,signal,sys
from api.application_runtime_config import load_config
from api.application_task_service import ApplicationService
c=load_config(sys.argv[1]); s=ApplicationService(c)
def die(*args,**kwargs): os.kill(os.getpid(),signal.SIGKILL)
setattr(s,sys.argv[3],die)
s.submit(json.loads(sys.argv[2]),c.principal('publisher'))
"""
    def kill_at(request, method):
        return subprocess.run([sys.executable,"-c",script,str(config_path),json.dumps(request),method],
                              cwd=ROOT,env=os.environ,capture_output=True,text=True,timeout=30)
    first = kill_at(victim,"_finish_task")
    assert first.returncode == -signal.SIGKILL, first.stderr
    assert service.get(victim["task_id"],config.principal("publisher"))["task_state"] == "RUNNING"
    observed = service.inspect_recovery(victim["task_id"],cid,config.principal("publisher"))
    assert observed["decision"] == "finalize_existing"
    recovery = _request(_id(815),"recover",{
        "candidate_id":cid,"interrupted_task_id":victim["task_id"],"decision":"finalize_existing",
        "expected_evidence_sha":observed["evidence_sha"],"approval_id":aid})
    second = kill_at(recovery,"_finish_recovery")
    assert second.returncode == -signal.SIGKILL, second.stderr
    with patch.object(service.files,"publish",side_effect=AssertionError("rename replayed")):
        final = service.submit(recovery,config.principal("publisher"))
    assert final["task_state"] == "SUCCEEDED"
    assert service.submit(recovery,config.principal("publisher")) == final
    with sqlite3.connect(config.ledger_path) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("SELECT count(*) FROM resource_owners").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM recovery_attempts").fetchone() == (1,)
    assert (config.artifact_root/"published"/cid/"display.sqlite").exists()


def test_real_http_routes_default_create_query_and_scope(runtime, tmp_path):
    import socket
    import sys
    import time
    import urllib.request
    import urllib.error
    service, config = runtime
    sock = socket.socket(); sock.bind(("127.0.0.1",0)); port=sock.getsockname()[1]; sock.close()
    home=tmp_path/"http-home"; home.mkdir()
    env=dict(os.environ, HERMES_HOME=str(home), HERMES_WEBUI_STATE_DIR=str(home/"webui"),
             HERMES_WEBUI_HOST="127.0.0.1", HERMES_WEBUI_PORT=str(port),
             APPLICATION_V2_CONFIG=str(config.ledger_path.parent/"application.json"),
             APPLICATION_V2_WEB_PRINCIPAL="creator", HERMES_WEBUI_TEST_NETWORK_BLOCK="1")
    env.pop("HERMES_WEBUI_PASSWORD",None)
    log=open(tmp_path/"http-server.log","w")
    proc=subprocess.Popen([sys.executable,"server.py"],cwd=ROOT,env=env,stdout=log,stderr=log)
    base=f"http://127.0.0.1:{port}"
    def call(path, body=None, cookie=None):
        headers={"Origin":base,"Content-Type":"application/json"}
        if cookie: headers["Cookie"]=cookie
        req=urllib.request.Request(base+path,data=None if body is None else json.dumps(body).encode(),headers=headers)
        try:
            with urllib.request.urlopen(req,timeout=10) as response: return response.status,json.load(response)
        except urllib.error.HTTPError as response:
            return response.code,json.load(response)
    try:
        deadline=time.monotonic()+20
        while True:
            if proc.poll() is not None: pytest.fail((tmp_path/"http-server.log").read_text())
            try:
                status,payload=call("/api/application/tasks")
                break
            except (ConnectionError,urllib.error.URLError):
                if time.monotonic()>deadline: raise
                time.sleep(.1)
        assert status==200,payload
        request=_request(_id(990),"create",{"candidate_id":_id(991)})
        status,created=call("/api/application/tasks",request)
        assert status==200,created
        assert created["task_state"]=="SUCCEEDED"
        assert call("/api/application/tasks/"+request["task_id"])[1]==created
        # A profile-selection cookie does not become a privileged principal.
        status,denied=call("/api/application/tasks",_request(_id(992),"publish",{
            "candidate_id":_id(991),"approval_id":_id(993)}),"hermes_profile=publisher")
        assert status==403,denied
        assert denied["issue"]["code"]=="PERMISSION_DENIED"
    finally:
        proc.terminate()
        try: proc.wait(timeout=10)
        except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=10)
        log.close()


def test_same_process_interrupted_create_finalizes_complete_artifact(runtime, monkeypatch):
    service, config = runtime
    from api.application_operation_issue import issue
    creator = config.principal("creator")
    cid = _id(1301)
    request = _request(_id(1302), "create", {"candidate_id": cid})
    original = service.files.create
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise issue("OUTCOME_UNKNOWN", "candidate", cid, "injected after files")
    monkeypatch.setattr(service.files, "create", interrupted)
    with pytest.raises(ApplicationIssue): service.submit(request, creator)
    observed = service.inspect_recovery(request["task_id"], cid, creator)
    assert observed["decision"] == "finalize_existing"
    assert observed["evidence"]["owner"]["owner_state"] == "UNRESOLVED"
    recovery = _request(_id(1303), "recover", {"candidate_id":cid,"interrupted_task_id":request["task_id"],
        "decision":"finalize_existing","expected_evidence_sha":observed["evidence_sha"],"approval_id":None})
    service.submit(recovery, creator)
    assert service.ledger.lookup(request["task_id"], creator)["state"] == "SUCCEEDED"
    assert service.ledger.head(cid)["state"] == "VERIFIED"


def test_recovery_cannot_substitute_approval(runtime, monkeypatch):
    service, config = runtime
    from api.application_operation_issue import issue
    cid, aid = _id(1401), _id(1402)
    created = service.submit(_request(_id(1403),"create",{"candidate_id":cid}),config.principal("creator"))["result"]
    service.submit(_request(_id(1404),"approve",{"candidate_id":cid,"approval_id":aid,"manifest_sha":created["manifest_sha"],"policy_sha":config.artifact_policy_sha,"reference":"review"}), config.principal("approver"))
    def interrupted(*args): raise issue("OUTCOME_UNKNOWN","candidate",cid,"injected")
    monkeypatch.setattr(service.files,"publish",interrupted)
    original=_request(_id(1405),"publish",{"candidate_id":cid,"approval_id":aid})
    with pytest.raises(ApplicationIssue):service.submit(original,config.principal("publisher"))
    observed=service.inspect_recovery(original["task_id"],cid,config.principal("publisher"))
    recovery=_request(_id(1406),"recover",{"candidate_id":cid,"interrupted_task_id":original["task_id"],"decision":"resume_publish","expected_evidence_sha":observed["evidence_sha"],"approval_id":_id(1499)})
    with pytest.raises(ApplicationIssue) as caught:service.submit(recovery,config.principal("publisher"))
    assert caught.value.code == "CONFLICT"
    assert service.ledger.lookup(recovery["task_id"],config.principal("publisher")) is None


def test_approval_missing_identity_rejected(runtime):
    service,config=runtime
    cid,aid=_id(1501),_id(1502)
    created=service.submit(_request(_id(1503),"create",{"candidate_id":cid}),config.principal("creator"))["result"]
    service.submit(_request(_id(1504),"approve",{"candidate_id":cid,"approval_id":aid,"manifest_sha":created["manifest_sha"],"policy_sha":config.artifact_policy_sha,"reference":"review"}),config.principal("approver"))
    path=service.files.approvals/(aid+".json")
    record=json.loads(path.read_text());del record["approver_principal_id"]
    path.write_bytes(canonical(record))
    with pytest.raises(ApplicationIssue): service.files.verify_approval(cid,aid)


def test_tampered_ddl_sha_and_symlink_rejected(runtime):
    service,config=runtime
    with service.ledger.session() as db:db.execute("UPDATE meta SET value='bad' WHERE key='ddl_sha'")
    with pytest.raises(ApplicationIssue):
        with service.ledger.session(readonly=True):pass
    original=config.ledger_path.with_suffix(".real")
    config.ledger_path.rename(original);config.ledger_path.symlink_to(original)
    with pytest.raises(ApplicationIssue):
        with service.ledger.session(readonly=True):pass


def test_writable_artifact_root_rejected(runtime):
    service,config=runtime
    service.files.root.chmod(0o777)
    with pytest.raises(ApplicationIssue):service.submit(_request(_id(1601),"create",{"candidate_id":_id(1602)}),config.principal("creator"))


def test_cleanup_database_failure_does_not_block_same_process_recovery(runtime, monkeypatch):
    service,config=runtime
    from api.application_operation_issue import issue
    cid=_id(1701); original=_request(_id(1702),"create",{"candidate_id":cid})
    creator=config.principal("creator")
    create=service.files.create; transaction=service.ledger.transaction
    def interrupted(*args):
        create(*args)
        def unavailable(*args):raise sqlite3.OperationalError("injected cleanup unavailable")
        monkeypatch.setattr(service.ledger,"transaction",unavailable)
        raise issue("OUTCOME_UNKNOWN","candidate",cid,"injected interruption")
    monkeypatch.setattr(service.files,"create",interrupted)
    with pytest.raises(ApplicationIssue):service.submit(original,creator)
    monkeypatch.setattr(service.ledger,"transaction",transaction)
    assert service.ledger.owner("candidate:"+cid)["owner_state"] == "ACTIVE"
    observed=service.inspect_recovery(original["task_id"],cid,creator)
    assert observed["decision"] == "finalize_existing"
    service.submit(_request(_id(1703),"recover",{"candidate_id":cid,"interrupted_task_id":original["task_id"],"decision":"finalize_existing","expected_evidence_sha":observed["evidence_sha"],"approval_id":None}),creator)
    assert service.ledger.lookup(original["task_id"],creator)["state"] == "SUCCEEDED"


def test_relative_storage_and_symlink_ancestor_rejected(tmp_path):
    from api.application_storage_trust import trusted_path
    data=_config_data(tmp_path,{"creator":{"permissions":["create"],"task_read_all":False}})
    data["ledger_path"]="relative.sqlite"
    path=tmp_path/"config.json";_write_config(path,data)
    with pytest.raises(ApplicationIssue):load_config(path)
    actual=tmp_path/"actual";actual.mkdir();(actual/"child").mkdir()
    alias=tmp_path/"alias";alias.symlink_to(actual,target_is_directory=True)
    with pytest.raises(ApplicationIssue):trusted_path(alias/"child",directory=True)


def test_live_other_process_cleanup_failure_can_be_recovered(runtime):
    import sys
    service,config=runtime
    cid=_id(1801); original=_request(_id(1802),"create",{"candidate_id":cid})
    script="""
import json,sys,sqlite3
from api.application_runtime_config import load_config
from api.application_task_service import ApplicationService
from api.application_operation_issue import issue
c=load_config(sys.argv[1]);s=ApplicationService(c);create=s.files.create
def interrupted(*args):
    create(*args)
    def unavailable(*args):raise sqlite3.OperationalError('cleanup unavailable')
    s.ledger.transaction=unavailable
    raise issue('OUTCOME_UNKNOWN',message='injected')
s.files.create=interrupted
try:s.submit(json.loads(sys.argv[2]),c.principal('creator'))
except Exception:pass
print('idle',flush=True)
sys.stdin.readline()
"""
    proc=subprocess.Popen([sys.executable,'-c',script,str(config.ledger_path.parent/'application.json'),json.dumps(original)],cwd=ROOT,env=os.environ.copy(),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert proc.stdout.readline().strip()=='idle'
        assert proc.poll() is None
        owner=service.ledger.owner('candidate:'+cid)
        assert owner['owner_state']=='ACTIVE' and owner['pid']==proc.pid
        observed=service.inspect_recovery(original['task_id'],cid,config.principal('creator'))
        assert observed['decision']=='finalize_existing'
        service.submit(_request(_id(1803),'recover',{'candidate_id':cid,'interrupted_task_id':original['task_id'],'decision':'finalize_existing','expected_evidence_sha':observed['evidence_sha'],'approval_id':None}),config.principal('creator'))
        assert service.ledger.lookup(original['task_id'],config.principal('creator'))['state']=='SUCCEEDED'
        assert proc.poll() is None
    finally:
        proc.communicate(input='exit\n',timeout=10)


def test_initialization_never_writes_through_symlink(tmp_path):
    from api.application_storage_trust import trusted_mkdir
    actual=tmp_path/'actual';actual.mkdir()
    alias=tmp_path/'alias';alias.symlink_to(actual,target_is_directory=True)
    with pytest.raises(ApplicationIssue):trusted_mkdir(alias/'must-not-exist'/'child')
    assert not (actual/'must-not-exist').exists()
