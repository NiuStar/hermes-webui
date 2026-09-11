"""Synchronous default application v2 lifecycle service."""

from contextlib import contextmanager
import hashlib
import json

from api.application_artifact_files import ArtifactFiles
from api.application_business_ledger import BusinessLedger, encode
from api.application_operation_issue import ApplicationIssue, envelope, issue
from api.application_protocol import MODE, digest, id32, parse_request
from api.application_resource_owner import ResourceOwner, owner_alive, process_identity

STEPS = {"create": "create_artifact", "approve": "write_approval", "publish": "publish_rename"}
EXPECTED_HEAD = {"create": None, "approve": "VERIFIED", "publish": "APPROVED"}
BUSINESS = {"create": "VERIFIED", "approve": "APPROVAL_DURABLE",
            "publish": "PUBLISHED_UNACTIVATED"}
EVENT_STATE = {"create": "VERIFIED", "approve": "APPROVED",
               "publish": "PUBLISHED_UNACTIVATED"}


class ApplicationService:
    def __init__(self, config):
        self.config = config
        self.ledger = BusinessLedger(config.ledger_path, config.busy_timeout_ms)
        self.files = ArtifactFiles(config.artifact_root, policy=config.artifact_policy,
                                   policy_sha=config.artifact_policy_sha,
                                   creator_commit=config.creator_commit,
                                   max_bytes=config.artifact_max_bytes,
                                   trusted_approvers={name for name, item in config.principals.items()
                                                      if "approve" in item.get("permissions", [])})
        self.resources = ResourceOwner(self.files.locks)

    def initialize(self):
        self.files.initialize()
        BusinessLedger.initialize(self.config.ledger_path, self.config.busy_timeout_ms)
        return {"status": "INITIALIZED", "runtime_format": MODE, "quota_enabled": False,
                "activated": False}

    def submit(self, raw, principal):
        return self._submit_request(parse_request(raw), principal)

    @contextmanager
    def _execution(self, request):
        # Acquire before registering ownership: a BUSY duplicate must never release another call.
        with self.resources.acquire(self._keys(request)):
            try:
                yield
            except BaseException:
                self._mark_interrupted(request)
                raise

    def _mark_interrupted(self, request):
        try:
            current = process_identity()
            def action(db):
                db.execute("UPDATE resource_owners SET owner_state='UNRESOLVED',boot_id=NULL,pid=NULL,start_ticks=NULL,execution_group=NULL,phase='INTERRUPTED' WHERE executor_request_id=? AND pid=? AND start_ticks=? AND boot_id=?",
                           (request.task_id,current["pid"],current["start_ticks"],current["boot_id"]))
            def verify(db):
                return db.execute("SELECT 1 FROM resource_owners WHERE executor_request_id=? AND pid=? AND start_ticks=?", (request.task_id,current["pid"],current["start_ticks"])).fetchone() is None
            self.ledger.transaction(action, verify)
        except Exception:
            # Future recovery must acquire the cross-process writer lock before taking over.
            pass

    def _submit_request(self, request, principal):
        self.files.verify_roots()
        principal.require(request.operation)
        prior = self.ledger.lookup(request.task_id, principal, request.request_sha)
        if prior is not None:
            if request.operation == "recover" and prior["state"] == "RUNNING":
                if prior["principal_id"] != principal.name:
                    raise issue("PERMISSION_DENIED", message="recovery belongs to another principal")
                return self._recover(request, principal, resume=True)
            return self._stored_envelope(prior)
        if request.operation == "recover":
            return self._recover(request, principal)
        return self._ordinary(request, principal)

    def get(self, task_id, principal):
        id32(task_id, "task_id")
        row = self.ledger.lookup(task_id, principal)
        if row is None:
            raise issue("NOT_FOUND", "task", None, "task not found")
        if row["record_format"] != MODE:
            raise issue("UNSUPPORTED_VERSION", "task", task_id, "unknown or legacy result format")
        return self._stored_envelope(row)

    def list(self, principal, limit=50, offset=0):
        return {"tasks": self.ledger.list_tasks(principal, limit, offset),
                "quota": {"enabled": False}}

    def _stored_envelope(self, row):
        if row["result"] is not None:
            return envelope(row["id"], accepted=True, task_state=row["state"], result=row["result"])
        problem = issue("OUTCOME_UNKNOWN", "task", row["id"], "task requires status or recovery inspection")
        problem = ApplicationIssue(problem.code, problem.scope_type, problem.scope_id,
                                   "inspect_recovery", problem.message, problem.http_status,
                                   problem.cli_status)
        return envelope(row["id"], accepted=True, task_state=row["state"], problem=problem)

    def _keys(self, request):
        keys = ["candidate:" + request.parameters["candidate_id"]]
        approval_id = request.parameters.get("approval_id")
        if approval_id:
            keys.append("approval:" + str(approval_id))
        return keys

    def _ordinary(self, request, principal):
        cid = str(request.parameters["candidate_id"])
        resource_key = "candidate:" + cid
        tool = None
        if request.operation == "create" and self.config.fixed_tool is not None:
            from api.application_fixed_tool import FixedPolicyTool
            tool = FixedPolicyTool(self.config.fixed_tool, policy_sha=self.config.artifact_policy_sha,
                                   source_commit=self.config.creator_commit)
            tool.preflight()
        with self._execution(request):
            self._admit_task(request, principal, resource_key)
            self._start_task(request, resource_key)
            payload = {"operation": request.operation, "parameters": dict(request.parameters),
                       "policy_sha": self.config.artifact_policy_sha}
            self._intent(request.task_id, STEPS[request.operation], payload,
                         source=str(self.files.candidates / cid),
                         target=str(self.files.published / cid) if request.operation == "publish" else None)
            if request.operation == "publish":
                self._publish_intent(request, self.files.verify_approval(cid, str(request.parameters["approval_id"])))
            try:
                if request.operation == "create":
                    tool_evidence = tool.run(request.task_id, self.files, self._intent) if tool else {}
                    manifest, manifest_sha = self.files.create(cid, principal)
                    evidence = {"manifest_sha": manifest_sha, "db_sha": manifest["db_sha"],
                                "db_bytes": manifest["db_bytes"], **tool_evidence}
                elif request.operation == "approve":
                    record, approval_sha = self.files.approve(request.parameters, principal)
                    evidence = {"manifest_sha": record["manifest_sha"],
                                "approval_id": record["approval_id"], "approval_sha": approval_sha}
                else:
                    evidence = self.files.publish(cid, str(request.parameters["approval_id"]))
            except ApplicationIssue:
                raise
            except BaseException as exc:
                raise issue("OUTCOME_UNKNOWN", "candidate", cid,
                            "filesystem operation outcome requires inspection") from exc
            result = self._result(request.task_id, request.operation, cid,
                                  BUSINESS[request.operation], evidence)
            self._finish_task(request, result, evidence, resource_key)
            stored = self.ledger.lookup(request.task_id, principal, request.request_sha)
            return self._stored_envelope(stored)

    def _admit_task(self, request, principal, resource_key):
        cid = str(request.parameters["candidate_id"])
        identity = process_identity()
        def action(db):
            head = self.ledger.head(cid, db)
            expected = EXPECTED_HEAD[request.operation]
            if (head is None) != (expected is None) or (head and head["state"] != expected):
                raise issue("CONFLICT", "candidate", cid, "candidate state conflict")
            db.execute("INSERT INTO request_ids VALUES(?,?,?,?,?)",
                       (request.task_id, "task", request.request_sha, principal.name, MODE))
            db.execute("INSERT INTO tasks(id,candidate_id,operation,parameters,state,policy_sha) VALUES(?,?,?,?,?,?)",
                       (request.task_id, cid, request.operation, encode(dict(request.parameters)),
                        "RESERVED", self.config.artifact_policy_sha))
            db.execute("INSERT INTO resource_owners VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (resource_key, request.task_id, request.task_id, 0, identity["owner_state"],
                        identity["boot_id"], identity["pid"], identity["start_ticks"],
                        identity["execution_group"], "ADMITTED"))
        def verify(db):
            row = db.execute("SELECT r.request_sha,r.principal_id,t.state,o.executor_request_id,o.generation "
                             "FROM request_ids r JOIN tasks t ON t.id=r.id JOIN resource_owners o ON o.root_task_id=t.id "
                             "WHERE r.id=?", (request.task_id,)).fetchone()
            return row is not None and tuple(row) == (request.request_sha, principal.name, "RESERVED", request.task_id, 0)
        self.ledger.transaction(action, verify)

    def _start_task(self, request, resource_key):
        cid = str(request.parameters["candidate_id"])
        def action(db):
            row = db.execute("UPDATE tasks SET state='RUNNING',revision=revision+1 WHERE id=? AND state='RESERVED' AND revision=0",
                             (request.task_id,))
            if row.rowcount != 1:
                raise issue("CONFLICT", "task", request.task_id, "task cannot start")
            db.execute("UPDATE resource_owners SET phase='RUNNING' WHERE resource_key=? AND executor_request_id=? AND generation=0",
                       (resource_key, request.task_id))
            if request.operation == "create":
                event = self.ledger.event(cid, "BUILDING", {"task_id": request.task_id}, None)
                self.ledger.put_event(db, event)
        def verify(db):
            task = db.execute("SELECT state,revision FROM tasks WHERE id=?", (request.task_id,)).fetchone()
            owner = db.execute("SELECT phase FROM resource_owners WHERE resource_key=?", (resource_key,)).fetchone()
            head = self.ledger.head(cid, db)
            head_ok = request.operation != "create" or (head is not None and head["state"] == "BUILDING")
            return (task is not None and tuple(task)) == ("RUNNING", 1) and (owner is not None and tuple(owner)) == ("RUNNING",) and head_ok
        self.ledger.transaction(action, verify)

    def _intent(self, request_id, step, payload, source=None, target=None):
        raw = encode(payload)
        intent_id = hashlib.sha256(f"{request_id}:{step}".encode()).hexdigest()
        with self.ledger.session(readonly=True) as db:
            old = db.execute("SELECT payload FROM operation_intents WHERE request_id=? AND step=?",
                             (request_id, step)).fetchone()
        if old is not None:
            if old[0] != raw:
                raise issue("CONFLICT", "task", request_id, "intent payload conflict")
            return
        def action(db):
            db.execute("INSERT INTO operation_intents VALUES(?,?,?,?,?,?,?,?)",
                       (intent_id, request_id, step, source, target, digest(payload), raw, "PREPARED"))
        def verify(db):
            row = db.execute("SELECT payload,state FROM operation_intents WHERE request_id=? AND step=?",
                             (request_id, step)).fetchone()
            return (row is not None and tuple(row)) == (raw, "PREPARED")
        self.ledger.transaction(action, verify)

    def _publish_intent(self, request, evidence):
        cid = str(request.parameters["candidate_id"])
        old = self.ledger.head(cid)
        if old and old["state"] == "PUBLISH_INTENT":
            with self.ledger.session(readonly=True) as db:
                raw = db.execute("SELECT record FROM events WHERE candidate_id=? AND seq=?", (cid, old["seq"])).fetchone()
            if raw and json.loads(raw[0])["payload"].get("task_id") == request.task_id:
                return
            raise issue("CONFLICT", "candidate", cid, "another publication intent")
        if not old or old["state"] != "APPROVED":
            raise issue("CONFLICT", "candidate", cid, "publication needs approval")
        event = self.ledger.event(cid, "PUBLISH_INTENT", {"task_id": request.task_id, **evidence}, old)
        def action(db):
            if self.ledger.head(cid, db) != old:
                raise issue("CONFLICT", "candidate", cid, "publication head changed")
            self.ledger.put_event(db, event)
        def verify(db):
            row = db.execute("SELECT record FROM events WHERE candidate_id=? AND seq=?", (cid, event["seq"])).fetchone()
            return row is not None and row[0] == encode(event) and self.ledger.head(cid, db)["record_sha"] == event["record_sha"]
        self.ledger.transaction(action, verify)

    def _result(self, task_id, operation, candidate_id, business_status, evidence):
        return {"mode": MODE, "task_id": task_id, "task_state": "SUCCEEDED",
                "operation": operation, "business_status": business_status, "code": "OK",
                "candidate_id": candidate_id, "approval_id": evidence.get("approval_id"),
                "manifest_sha": evidence.get("manifest_sha"), "approval_sha": evidence.get("approval_sha"),
                "completion_record_sha": None, "evidence_sha": digest(evidence), "activated": False}

    def _finish_task(self, request, result, evidence, resource_key):
        cid = str(request.parameters["candidate_id"])
        old = self.ledger.head(cid)
        event = self.ledger.event(cid, EVENT_STATE[request.operation],
                                  {"task_id": request.task_id, **evidence}, old)
        if request.operation == "publish":
            result["completion_record_sha"] = event["record_sha"]
        _, audit_sha, audit_path = self.files.audit(request.task_id, request.request_sha, result)
        def action(db):
            if self.ledger.head(cid, db) != old:
                raise issue("CONFLICT", "candidate", cid, "event head changed")
            row = db.execute("UPDATE tasks SET state='SUCCEEDED',result=?,revision=revision+1,original_audit_sha=? "
                             "WHERE id=? AND state='RUNNING' AND revision=1",
                             (encode(result), audit_sha, request.task_id))
            if row.rowcount != 1:
                raise issue("CONFLICT", "task", request.task_id, "task finalization conflict")
            db.execute("INSERT INTO audit_refs VALUES(?,?,?,?,?,?,?)",
                       (audit_sha, request.task_id, "ORIGINAL", audit_path, audit_sha, None, "COMPLETE"))
            self.ledger.put_event(db, event)
            db.execute("DELETE FROM resource_owners WHERE resource_key=? AND executor_request_id=? AND generation=0",
                       (resource_key, request.task_id))
            db.execute("UPDATE operation_intents SET state='VERIFIED' WHERE request_id=? AND step=?",
                       (request.task_id, STEPS[request.operation]))
        def verify(db):
            task = db.execute("SELECT state,result,original_audit_sha FROM tasks WHERE id=?", (request.task_id,)).fetchone()
            owner = db.execute("SELECT 1 FROM resource_owners WHERE resource_key=?", (resource_key,)).fetchone()
            audit = db.execute("SELECT sha FROM audit_refs WHERE owner_id=?", (request.task_id,)).fetchone()
            head = self.ledger.head(cid, db)
            return ((task is not None and tuple(task)) == ("SUCCEEDED", encode(result), audit_sha) and owner is None
                    and (audit is not None and tuple(audit)) == (audit_sha,) and head is not None
                    and head["state"] == EVENT_STATE[request.operation])
        self.ledger.transaction(action, verify)

    def inspect_recovery(self, interrupted_task_id, candidate_id, principal, *, _locked=False):
        id32(interrupted_task_id, "interrupted_task_id")
        id32(candidate_id, "candidate_id")
        if not _locked:
            # Every writer in this v2 implementation is synchronous under this OS lock.
            # Owning it proves no operation is running, even if its host server PID lives.
            with self.resources.acquire(["candidate:" + candidate_id]):
                return self.inspect_recovery(interrupted_task_id, candidate_id, principal, _locked=True)
        id32(interrupted_task_id, "interrupted_task_id")
        id32(candidate_id, "candidate_id")
        task = self.ledger.lookup(interrupted_task_id, principal)
        if task is None or task["kind"] != "task" or task["candidate_id"] != candidate_id:
            raise issue("NOT_FOUND", "task", None, "task not found")
        from api.application_fixed_tool import require_tool_descendants_gone
        require_tool_descendants_gone(self.ledger, interrupted_task_id)
        source, target = self.files.path_state(candidate_id)
        owner = self.ledger.owner("candidate:" + candidate_id)
        with self.ledger.session(readonly=True) as db:
            intent = db.execute("SELECT step,payload,state FROM operation_intents WHERE request_id=? ORDER BY rowid DESC LIMIT 1",
                                (interrupted_task_id,)).fetchone()
            head = self.ledger.head(candidate_id, db)
        evidence = {"original_request_sha": task["request_sha"], "task_revision": task["revision"],
                    "policy_sha": task["policy_sha"], "head": head,
                    "source": source, "target": target, "owner": owner,
                    "intent": dict(intent) if intent else None, "exclusive_writer_lock": True,
                    "original_audit": self.files.audit_snapshot(interrupted_task_id)}
        if task["operation"] == "create" and source and source["state"] == "VERIFIED":
            from api.application_fixed_tool import tool_evidence
            evidence["tool"] = tool_evidence(self.ledger, self.files, interrupted_task_id)
        decision = self._recovery_decision(task, evidence)
        return {"mode": MODE, "candidate_id": candidate_id,
                "interrupted_task_id": interrupted_task_id, "observed_task_state": task["state"],
                "decision": decision, "evidence_sha": digest(evidence), "evidence": evidence}

    def _recovery_decision(self, task, evidence):
        if task["state"] in {"SUCCEEDED", "FAILED"}:
            return "already_terminal"
        owner = evidence["owner"]
        if owner and owner_alive(owner) and not evidence.get("exclusive_writer_lock", False):
            return "running"
        source, target = evidence["source"], evidence["target"]
        if task["operation"] == "create" and target is None:
            return "finalize_existing" if source and source["state"] == "VERIFIED" else "close_failed"
        if task["operation"] == "approve" and source and target is None:
            aid = task["parameters"].get("approval_id")
            if aid and (self.files.approvals / (str(aid) + ".json")).exists():
                return "finalize_existing"
        if task["operation"] == "publish":
            if source and target is None:
                return "resume_publish"
            if source is None and target:
                return "finalize_existing"
        return "operator_review"

    def _recover(self, request, principal, resume=False):
        p = request.parameters
        cid, original_id = str(p["candidate_id"]), str(p["interrupted_task_id"])
        original = self.ledger.lookup(original_id, principal)
        if original is None or original["kind"] != "task" or original["candidate_id"] != cid:
            raise issue("NOT_FOUND", "task", None, "task not found")
        if p["decision"] != "close_failed":
            expected_approval = original["parameters"].get("approval_id")
            if p["approval_id"] != expected_approval:
                raise issue("CONFLICT", "candidate", cid, "approval differs from original request")
        with self._execution(request):
            observed = self.inspect_recovery(original_id, cid, principal, _locked=True)
            if not resume and observed["evidence_sha"] != p["expected_evidence_sha"]:
                raise issue("CONFLICT", "candidate", cid, "recovery evidence changed")
            if observed["decision"] == "running":
                raise issue("BUSY", "candidate", cid, "original executor is still alive")
            if resume:
                owner = observed["evidence"]["owner"]
                if not owner or owner["executor_request_id"] != request.task_id:
                    raise issue("CONFLICT", "candidate", cid, "recovery owner mismatch")
                allowed = {str(p["decision"])}
                if p["decision"] == "resume_publish":
                    allowed.add("finalize_existing")
                if observed["decision"] not in allowed:
                    raise issue("CONFLICT", "candidate", cid, "recovery state no longer matches intent")
                identity = process_identity()
                def reclaim(db):
                    changed = db.execute("UPDATE resource_owners SET generation=generation+1,boot_id=?,pid=?,start_ticks=?,execution_group=?,owner_state='ACTIVE' WHERE resource_key=? AND generation=? AND executor_request_id=?",
                                         (identity["boot_id"], identity["pid"], identity["start_ticks"], identity["execution_group"], "candidate:"+cid, owner["generation"], request.task_id))
                    if changed.rowcount != 1:
                        raise issue("CONFLICT", "candidate", cid, "recovery owner changed")
                def reclaimed(db):
                    row = db.execute("SELECT generation,boot_id,pid,start_ticks FROM resource_owners WHERE resource_key=? AND executor_request_id=?", ("candidate:"+cid, request.task_id)).fetchone()
                    return row is not None and tuple(row) == (owner["generation"]+1, identity["boot_id"], identity["pid"], identity["start_ticks"])
                self.ledger.transaction(reclaim, reclaimed)
            elif observed["decision"] != p["decision"]:
                raise issue("CONFLICT", "candidate", cid, "recovery decision denied")
            else:
                self._admit_recovery(request, principal, original, observed)
            step = {"close_failed": "recover_close_failed", "resume_publish": "recover_publish_rename",
                    "finalize_existing": "recover_finalize"}[str(p["decision"])]
            self._intent(request.task_id, step, {"decision": p["decision"],
                         "evidence_sha": p["expected_evidence_sha"], "original_task_id": original_id})
            try:
                if p["decision"] == "close_failed":
                    evidence = {"original_task_id": original_id, "closed": "FAILED"}
                    business = "RECOVERED_FAILED"
                    original_state = "FAILED"
                elif original["operation"] == "create":
                    manifest, manifest_sha = self.files.verify(self.files.candidates, cid)
                    evidence = {"manifest_sha": manifest_sha, "db_sha": manifest["db_sha"], "db_bytes": manifest["db_bytes"],
                                **observed["evidence"].get("tool", {})}
                    business, original_state = "VERIFIED", "SUCCEEDED"
                elif original["operation"] == "approve":
                    evidence = self.files.verify_approval(cid, str(p["approval_id"]))
                    business, original_state = "APPROVAL_DURABLE", "SUCCEEDED"
                elif p["decision"] == "resume_publish":
                    if resume and observed["decision"] == "finalize_existing":
                        evidence = self.files.verify_approval(cid, str(p["approval_id"]), self.files.published)
                    else:
                        evidence = self.files.publish(cid, str(p["approval_id"]))
                    business, original_state = "PUBLISHED_UNACTIVATED", "SUCCEEDED"
                else:
                    evidence = self.files.verify_approval(cid, str(p["approval_id"]), self.files.published)
                    business, original_state = "PUBLISHED_UNACTIVATED", "SUCCEEDED"
                evidence["original_audit"] = observed["evidence"]["original_audit"]
                result = self._result(request.task_id, "recover", cid, business, evidence)
            except ApplicationIssue as exc:
                if p["decision"] != "resume_publish" and exc.code in {"INTEGRITY_ERROR", "CONFLICT", "NOT_FOUND"}:
                    self._fail_recovery(request, original, exc, step)
                    stored = self.ledger.lookup(request.task_id, principal, request.request_sha)
                    return self._stored_envelope(stored)
                raise
            # Audit/commit errors are uncertain, never close the attempt as a
            # validation failure after a possibly successful filesystem effect.
            self._finish_recovery(request, original, result, evidence, original_state, step)
            stored = self.ledger.lookup(request.task_id, principal, request.request_sha)
            return self._stored_envelope(stored)

    def _admit_recovery(self, request, principal, original, observed):
        cid, original_id = str(request.parameters["candidate_id"]), str(request.parameters["interrupted_task_id"])
        identity = process_identity()
        resource_key = "candidate:" + cid
        old_owner = observed["evidence"]["owner"]
        generation = (old_owner["generation"] + 1) if old_owner else 0
        def action(db):
            db.execute("INSERT INTO request_ids VALUES(?,?,?,?,?)",
                       (request.task_id, "recovery", request.request_sha, principal.name, MODE))
            db.execute("INSERT INTO recovery_attempts(id,original_task_id,state,decision,expected_evidence_sha) VALUES(?,?,?,?,?)",
                       (request.task_id, original_id, "RUNNING", request.parameters["decision"], request.parameters["expected_evidence_sha"]))
            if old_owner:
                row = db.execute("UPDATE resource_owners SET executor_request_id=?,generation=?,owner_state='ACTIVE',"
                                 "boot_id=?,pid=?,start_ticks=?,execution_group=?,phase='RECOVERY' "
                                 "WHERE resource_key=? AND generation=?",
                                 (request.task_id, generation, identity["boot_id"], identity["pid"], identity["start_ticks"],
                                  identity["execution_group"], resource_key, old_owner["generation"]))
                if row.rowcount != 1:
                    raise issue("CONFLICT", "candidate", cid, "owner changed")
            else:
                db.execute("INSERT INTO resource_owners VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (resource_key, original_id, request.task_id, generation, "ACTIVE", identity["boot_id"],
                            identity["pid"], identity["start_ticks"], identity["execution_group"], "RECOVERY"))
        def verify(db):
            attempt = db.execute("SELECT state,decision,expected_evidence_sha FROM recovery_attempts WHERE id=?",
                                 (request.task_id,)).fetchone()
            owner = db.execute("SELECT executor_request_id,generation FROM resource_owners WHERE resource_key=?",
                               (resource_key,)).fetchone()
            return (attempt is not None and tuple(attempt)) == ("RUNNING", request.parameters["decision"], request.parameters["expected_evidence_sha"]) and (owner is not None and tuple(owner)) == (request.task_id, generation)
        self.ledger.transaction(action, verify)

    def _finish_recovery(self, request, original, result, evidence, original_state, step):
        original_id, cid = str(request.parameters["interrupted_task_id"]), str(request.parameters["candidate_id"])
        audit_snapshot = self.files.audit_snapshot(original_id)
        if audit_snapshot != evidence.get("original_audit"):
            raise issue("CONFLICT", "audit", original_id, "original audit changed during recovery")
        repair = None
        if audit_snapshot is not None and not original.get("original_audit_sha"):
            repair_id = digest({"owner_id": request.task_id, **audit_snapshot})
            repair = (repair_id, request.task_id, "REPAIR", audit_snapshot["relative_path"],
                      audit_snapshot["sha"], None, audit_snapshot["record_state"])
        relation = repair[0] if repair else original.get("original_audit_sha")
        old_head = self.ledger.head(cid)
        event = self.ledger.event(cid, "FAILED" if original_state == "FAILED" else
                                  ("VERIFIED" if result["business_status"] == "VERIFIED" else "APPROVED" if result["business_status"] == "APPROVAL_DURABLE" else "PUBLISHED_UNACTIVATED"),
                                  {"task_id": original_id, "recovery_id": request.task_id, **evidence}, old_head)
        if result["business_status"] == "PUBLISHED_UNACTIVATED":
            result["completion_record_sha"] = event["record_sha"]
        _, audit_sha, audit_path = self.files.audit(request.task_id, request.request_sha, result,
                                                    "RECOVERY", relation)
        resource_key = "candidate:" + cid
        def action(db):
            if self.files.audit_snapshot(original_id) != audit_snapshot:
                raise issue("CONFLICT", "audit", original_id, "original audit changed before commit")
            if repair:
                db.execute("INSERT INTO audit_refs VALUES(?,?,?,?,?,?,?)", repair)
            if self.ledger.head(cid, db) != old_head:
                raise issue("CONFLICT", "candidate", cid, "recovery event head changed")
            original_result = dict(result, task_id=original_id, operation=original["operation"], task_state=original_state)
            row = db.execute("UPDATE tasks SET state=?,result=?,revision=revision+1,finalized_by_attempt_id=? "
                             "WHERE id=? AND state IN ('RESERVED','RUNNING') AND revision=?",
                             (original_state, encode(original_result), request.task_id, original_id, original["revision"]))
            if row.rowcount != 1:
                raise issue("CONFLICT", "task", original_id, "original task changed")
            db.execute("UPDATE recovery_attempts SET state='SUCCEEDED',result=?,recovery_audit_sha=?,revision=revision+1 "
                       "WHERE id=? AND state='RUNNING' AND revision=0", (encode(result), audit_sha, request.task_id))
            db.execute("INSERT INTO audit_refs VALUES(?,?,?,?,?,?,?)",
                       (audit_sha, request.task_id, "RECOVERY", audit_path, audit_sha,
                        relation, "COMPLETE"))
            self.ledger.put_event(db, event)
            db.execute("DELETE FROM resource_owners WHERE resource_key=? AND executor_request_id=?",
                       (resource_key, request.task_id))
            db.execute("UPDATE operation_intents SET state='VERIFIED' WHERE request_id=? AND step=?",
                       (request.task_id, step))
        def verify(db):
            attempt = db.execute("SELECT state,result,recovery_audit_sha FROM recovery_attempts WHERE id=?",
                                 (request.task_id,)).fetchone()
            task = db.execute("SELECT state,finalized_by_attempt_id FROM tasks WHERE id=?", (original_id,)).fetchone()
            owner = db.execute("SELECT 1 FROM resource_owners WHERE resource_key=?", (resource_key,)).fetchone()
            refs = [tuple(row) for row in db.execute("SELECT * FROM audit_refs WHERE owner_id=? ORDER BY kind", (request.task_id,))]
            expected_refs = sorted(([repair] if repair else []) + [(audit_sha, request.task_id, "RECOVERY", audit_path, audit_sha, relation, "COMPLETE")], key=lambda row: row[2])
            intent = db.execute("SELECT state FROM operation_intents WHERE request_id=? AND step=?", (request.task_id, step)).fetchone()
            return ((attempt is not None and tuple(attempt)) == ("SUCCEEDED", encode(result), audit_sha)
                    and (task is not None and tuple(task)) == (original_state, request.task_id) and owner is None
                    and refs == expected_refs and self.ledger.head(cid, db)["record_sha"] == event["record_sha"]
                    and intent is not None and intent[0] == "VERIFIED"
                    and self.files.audit_snapshot(original_id) == audit_snapshot)
        self.ledger.transaction(action, verify)

    def _fail_recovery(self, request, original, problem, step):
        original_id = str(request.parameters["interrupted_task_id"])
        cid = str(request.parameters["candidate_id"])
        result = {"mode": MODE, "task_id": request.task_id, "task_state": "FAILED",
                  "operation": "recover", "business_status": "UNCERTAIN", "code": problem.code,
                  "candidate_id": cid, "approval_id": request.parameters.get("approval_id"),
                  "manifest_sha": None, "approval_sha": None, "completion_record_sha": None,
                  "evidence_sha": digest({"error": problem.code, "original_task_id": original_id}),
                  "activated": False}
        _, audit_sha, audit_path = self.files.audit(request.task_id, request.request_sha, result,
                                                    "RECOVERY", original.get("original_audit_sha"))
        resource_key = "candidate:" + cid
        def action(db):
            row = db.execute("UPDATE recovery_attempts SET state='FAILED',result=?,recovery_audit_sha=?,revision=revision+1 "
                             "WHERE id=? AND state='RUNNING' AND revision=0",
                             (encode(result), audit_sha, request.task_id))
            if row.rowcount != 1:
                raise issue("CONFLICT", "task", request.task_id, "recovery attempt changed")
            db.execute("INSERT INTO audit_refs VALUES(?,?,?,?,?,?,?)",
                       (audit_sha, request.task_id, "RECOVERY", audit_path, audit_sha,
                        original.get("original_audit_sha"), "COMPLETE"))
            db.execute("UPDATE resource_owners SET executor_request_id=?,owner_state='UNRESOLVED',"
                       "boot_id=NULL,pid=NULL,start_ticks=NULL,execution_group=NULL,phase='RECOVERY_FAILED' "
                       "WHERE resource_key=? AND executor_request_id=?",
                       (original_id, resource_key, request.task_id))
            db.execute("UPDATE operation_intents SET state='VERIFIED' WHERE request_id=? AND step=?",
                       (request.task_id, step))
        def verify(db):
            attempt = db.execute("SELECT state,result,recovery_audit_sha FROM recovery_attempts WHERE id=?",
                                 (request.task_id,)).fetchone()
            task = db.execute("SELECT state,revision FROM tasks WHERE id=?", (original_id,)).fetchone()
            owner = db.execute("SELECT executor_request_id,owner_state FROM resource_owners WHERE resource_key=?",
                               (resource_key,)).fetchone()
            return ((attempt is not None and tuple(attempt)) == ("FAILED", encode(result), audit_sha)
                    and (task is not None and tuple(task)) == (original["state"], original["revision"])
                    and (owner is not None and tuple(owner)) == (original_id, "UNRESOLVED"))
        self.ledger.transaction(action, verify)