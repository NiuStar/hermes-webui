"""SQLite business ledger for application_runtime_v2."""

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from functools import lru_cache
from api.application_storage_trust import trusted_path, trusted_mkdir

from api.application_operation_issue import issue
from api.application_protocol import canonical

DDL = """
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE request_ids(
 id TEXT PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('task','recovery')),
 request_sha TEXT NOT NULL,principal_id TEXT NOT NULL,
 record_format TEXT NOT NULL CHECK(record_format IN ('application_budget_v1','application_runtime_v2')),
 UNIQUE(id,kind));
CREATE TABLE tasks(
 id TEXT PRIMARY KEY,candidate_id TEXT NOT NULL,operation TEXT NOT NULL,
 parameters TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('RESERVED','RUNNING','SUCCEEDED','FAILED')),
 result TEXT,revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),policy_sha TEXT NOT NULL,
 original_audit_sha TEXT,finalized_by_attempt_id TEXT,request_kind TEXT NOT NULL DEFAULT 'task' CHECK(request_kind='task'),
 FOREIGN KEY(id,request_kind) REFERENCES request_ids(id,kind),
 CHECK((state IN ('RESERVED','RUNNING') AND result IS NULL) OR
       (state IN ('SUCCEEDED','FAILED') AND result IS NOT NULL)));
CREATE TABLE recovery_attempts(
 id TEXT PRIMARY KEY,original_task_id TEXT NOT NULL REFERENCES tasks(id),
 state TEXT NOT NULL CHECK(state IN ('RESERVED','RUNNING','SUCCEEDED','FAILED')),
 decision TEXT NOT NULL,expected_evidence_sha TEXT NOT NULL,result TEXT,
 original_audit_sha TEXT,recovery_audit_sha TEXT,revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
 request_kind TEXT NOT NULL DEFAULT 'recovery' CHECK(request_kind='recovery'),
 FOREIGN KEY(id,request_kind) REFERENCES request_ids(id,kind),
 CHECK((state IN ('RESERVED','RUNNING') AND result IS NULL) OR
       (state IN ('SUCCEEDED','FAILED') AND result IS NOT NULL)));
CREATE TABLE resource_owners(
 resource_key TEXT PRIMARY KEY,root_task_id TEXT NOT NULL REFERENCES tasks(id),
 executor_request_id TEXT NOT NULL REFERENCES request_ids(id),generation INTEGER NOT NULL CHECK(generation>=0),
 owner_state TEXT NOT NULL CHECK(owner_state IN ('ACTIVE','UNRESOLVED')),
 boot_id TEXT,pid INTEGER,start_ticks INTEGER,execution_group TEXT,phase TEXT NOT NULL,
 CHECK((owner_state='ACTIVE' AND boot_id IS NOT NULL AND pid IS NOT NULL AND start_ticks IS NOT NULL AND pid>0 AND start_ticks>=0 AND execution_group IS NOT NULL) OR
       (owner_state='UNRESOLVED' AND boot_id IS NULL AND pid IS NULL AND start_ticks IS NULL AND execution_group IS NULL)));
CREATE TABLE operation_intents(
 id TEXT PRIMARY KEY,request_id TEXT NOT NULL REFERENCES request_ids(id),step TEXT NOT NULL,
 source TEXT,target TEXT,expected_sha TEXT,payload TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('PREPARED','VERIFIED')),UNIQUE(request_id,step));
CREATE TABLE audit_refs(
 id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES request_ids(id),
 kind TEXT NOT NULL CHECK(kind IN ('ORIGINAL','RECOVERY','REPAIR')),
 relative_path TEXT NOT NULL UNIQUE,sha TEXT NOT NULL,relates_to TEXT REFERENCES audit_refs(id),
 record_state TEXT NOT NULL CHECK(record_state IN ('COMPLETE','PARTIAL')));
CREATE TABLE events(candidate_id TEXT NOT NULL,seq INTEGER NOT NULL,record TEXT NOT NULL,
 PRIMARY KEY(candidate_id,seq));
CREATE TABLE candidate_heads(candidate_id TEXT PRIMARY KEY,seq INTEGER NOT NULL,
 record_sha TEXT NOT NULL,state TEXT NOT NULL);
CREATE TABLE operation_issues(scope_type TEXT NOT NULL,scope_id TEXT NOT NULL,code TEXT NOT NULL,
 request_id TEXT REFERENCES request_ids(id),evidence TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0,
 cleared INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(scope_type,scope_id,code));
CREATE UNIQUE INDEX one_active_candidate_task ON tasks(candidate_id)
 WHERE state IN ('RESERVED','RUNNING');
CREATE UNIQUE INDEX one_active_attempt ON recovery_attempts(original_task_id)
 WHERE state IN ('RESERVED','RUNNING');
CREATE INDEX tasks_state_id ON tasks(state,id);
CREATE INDEX request_owner ON request_ids(principal_id,id);
"""


def encode(value):
    return canonical(value).decode("utf-8")


@lru_cache(maxsize=1)
def _expected_schema():
    with sqlite3.connect(":memory:") as db:
        db.executescript(DDL)
        return db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()


class BusinessLedger:
    def __init__(self, path, busy_timeout_ms=5000):
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms

    @classmethod
    def initialize(cls, path, busy_timeout_ms=5000):
        path = Path(path)
        trusted_mkdir(path.parent)
        trusted_path(path.parent, directory=True)
        if path.exists() or path.is_symlink():
            raise issue("CONFLICT", "ledger", str(path), "ledger already exists")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        db = sqlite3.connect(path, isolation_level=None)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            if db.execute("PRAGMA journal_mode=WAL").fetchone() != ("wal",):
                raise issue("DEPENDENCY_UNAVAILABLE", "ledger", str(path), "WAL unavailable")
            db.executescript("BEGIN IMMEDIATE;\n" + DDL)
            db.execute("INSERT INTO meta VALUES('schema_version','application_business_v2')")
            db.execute("INSERT INTO meta VALUES('ddl_sha',?)", (hashlib.sha256(DDL.encode()).hexdigest(),))
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()
        return cls(path, busy_timeout_ms)

    @contextmanager
    def session(self, readonly=False):
        if not self.path.exists():
            raise issue("NOT_FOUND", "ledger", str(self.path), "v2 ledger not found")
        trusted_path(self.path.parent, directory=True)
        identity = trusted_path(self.path)
        uri = self.path.absolute().as_uri() + ("?mode=ro" if readonly else "?mode=rw")
        db = None
        try:
            db = sqlite3.connect(uri, uri=True, isolation_level=None,
                                 timeout=self.busy_timeout_ms / 1000)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            if not readonly:
                db.execute("PRAGMA synchronous=FULL")
            if db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] != "application_business_v2":
                raise issue("UNSUPPORTED_VERSION", "ledger", str(self.path), "not an application business v2 ledger")
            ddl_sha = db.execute("SELECT value FROM meta WHERE key='ddl_sha'").fetchone()
            actual_schema = [tuple(r) for r in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
            if (ddl_sha is None or ddl_sha[0] != hashlib.sha256(DDL.encode()).hexdigest()
                    or actual_schema != _expected_schema() or trusted_path(self.path) != identity):
                raise issue("INTEGRITY", "ledger", str(self.path), "ledger schema or identity mismatch")
            yield db
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise issue("BUSY", "ledger", str(self.path), "ledger is busy") from exc
            raise
        finally:
            if db is not None:
                db.close()

    def transaction(self, action, verify):
        db = None
        try:
            with self.session() as db:
                db.execute("BEGIN IMMEDIATE")
                value = action(db)
                db.execute("COMMIT")
            with self.session(readonly=True) as read:
                if not verify(read):
                    raise issue("OUTCOME_UNKNOWN", "ledger", str(self.path), "transaction readback mismatch")
            return value
        except sqlite3.IntegrityError as exc:
            # The session context has already closed and rolled back the connection.
            raise issue("CONFLICT", "ledger", str(self.path), "business constraint conflict") from exc
        except sqlite3.Error as exc:
            try:
                if db is not None and db.in_transaction:
                    db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            try:
                with self.session(readonly=True) as read:
                    if verify(read):
                        return None
            except BaseException:
                pass
            raise issue("OUTCOME_UNKNOWN", "ledger", str(self.path), "transaction outcome unknown") from exc

    def lookup(self, request_id, principal, request_sha=None):
        with self.session(readonly=True) as db:
            parent = db.execute("SELECT * FROM request_ids WHERE id=?", (request_id,)).fetchone()
            if parent is None:
                return None
            principal.require_read(parent["principal_id"])
            if request_sha is not None and parent["request_sha"] != request_sha:
                raise issue("CONFLICT", "request", request_id, "request ID has different content")
            table = "tasks" if parent["kind"] == "task" else "recovery_attempts"
            row = db.execute(f"SELECT * FROM {table} WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise issue("INTEGRITY_ERROR", "request", request_id, "orphan request ID")
            result = dict(row)
            result.update({"kind": parent["kind"], "principal_id": parent["principal_id"],
                           "request_sha": parent["request_sha"], "record_format": parent["record_format"]})
            for key in ("parameters", "result"):
                if key in result and result[key] is not None:
                    result[key] = json.loads(result[key])
            return result

    def owner(self, resource_key):
        with self.session(readonly=True) as db:
            row = db.execute("SELECT * FROM resource_owners WHERE resource_key=?", (resource_key,)).fetchone()
            return dict(row) if row else None

    def head(self, candidate_id, db=None):
        if db is None:
            with self.session(readonly=True) as read:
                return self.head(candidate_id, read)
        row = db.execute("SELECT * FROM candidate_heads WHERE candidate_id=?", (candidate_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def event(candidate_id, state, payload, previous):
        record = {"candidate_id": candidate_id, "seq": 1 if previous is None else previous["seq"] + 1,
                  "state": state, "payload": payload,
                  "previous_sha": None if previous is None else previous["record_sha"]}
        record["record_sha"] = hashlib.sha256(canonical(record)).hexdigest()
        return record

    @staticmethod
    def put_event(db, record):
        current = db.execute("SELECT seq,record_sha FROM candidate_heads WHERE candidate_id=?",
                             (record["candidate_id"],)).fetchone()
        if ((current is None and record["previous_sha"] is not None)
                or (current is not None and (current["record_sha"] != record["previous_sha"]
                                              or current["seq"] + 1 != record["seq"]))):
            raise issue("CONFLICT", "candidate", record["candidate_id"], "candidate head changed")
        db.execute("INSERT INTO events VALUES(?,?,?)",
                   (record["candidate_id"], record["seq"], encode(record)))
        db.execute("INSERT INTO candidate_heads VALUES(?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET "
                   "seq=excluded.seq,record_sha=excluded.record_sha,state=excluded.state",
                   (record["candidate_id"], record["seq"], record["record_sha"], record["state"]))

    def list_tasks(self, principal, limit=50, offset=0):
        with self.session(readonly=True) as db:
            where, args = ("", []) if principal.task_read_all else ("WHERE r.principal_id=?", [principal.name])
            rows = db.execute(f"SELECT t.id,t.candidate_id,t.operation,t.state,t.result,t.revision,r.principal_id "
                              f"FROM tasks t JOIN request_ids r ON r.id=t.id {where} "
                              "ORDER BY t.rowid DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
            return [dict(row, result=json.loads(row["result"]) if row["result"] else None) for row in rows]