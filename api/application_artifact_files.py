"""Actual SQLite candidate, approval, publication, and audit artifacts."""

import ctypes
import hashlib
import json
import os
import sqlite3
import stat
import time
from pathlib import Path
from api.application_storage_trust import trusted_path, trusted_mkdir

from api import _display_schema_ddl
from api.application_operation_issue import issue
from api.application_protocol import MODE, canonical, digest
from api.application_space_probe import from_os_error, require_writable
from api.display_bootstrap_artifact import PINNED_DDL_SHA, verify_frozen_database


def _identity(info):
    return {key: getattr(info, "st_" + key) for key in ("dev", "ino", "uid", "gid", "mode", "nlink")}


def _write_exclusive(path, value):
    raw = canonical(value)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        if os.write(fd, raw) != len(raw):
            raise OSError("short write")
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(path.parent)
    if read_record(path) != value:
        raise issue("INTEGRITY_ERROR", "artifact", str(path), "record readback failed")
    return hashlib.sha256(raw).hexdigest()


def read_record(path, limit=65536):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise issue("INTEGRITY_ERROR", "artifact", str(path), "unsafe record")
        raw = os.read(fd, limit + 1)
        after = os.fstat(fd)
        if _identity(before) != _identity(after) or canonical(json.loads(raw)) != raw:
            raise issue("INTEGRITY_ERROR", "artifact", str(path), "record changed or is noncanonical")
        return json.loads(raw)
    finally:
        os.close(fd)


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _rename_no_replace(source, target):
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    if result != 0:
        err = ctypes.get_errno()
        if err in {17, 39}:
            raise issue("CONFLICT", "candidate", source.name, "publish target exists")
        raise OSError(err, os.strerror(err), str(source))


class ArtifactFiles:
    def __init__(self, root, *, policy, policy_sha, creator_commit, max_bytes, trusted_approvers=()):
        self.root = Path(root)
        self.candidates = self.root / "candidates"
        self.published = self.root / "published"
        self.approvals = self.root / "approvals"
        self.audits = self.root / "audits"
        self.locks = self.root / "locks"
        self.policy = policy
        self.policy_sha = policy_sha
        self.creator_commit = creator_commit
        self.max_bytes = max_bytes
        self.trusted_approvers = frozenset(trusted_approvers)

    def initialize(self):
        trusted_mkdir(self.root)
        trusted_path(self.root, directory=True)
        for path in (self.candidates, self.published, self.approvals, self.audits, self.locks):
            trusted_mkdir(path)
            trusted_path(path, directory=True)

    def verify_roots(self):
        for path in (self.root, self.candidates, self.published, self.approvals, self.audits, self.locks):
            trusted_path(path, directory=True)

    def path_state(self, candidate_id):
        self.verify_roots()
        def inspect(base):
            path = base / candidate_id
            if not path.exists():
                return None
            try:
                manifest, manifest_sha = self.verify(base, candidate_id)
                return {"state": "VERIFIED", "manifest_sha": manifest_sha,
                        "db_sha": manifest["db_sha"]}
            except BaseException:
                info = path.lstat()
                return {"state": "PARTIAL", "identity": _identity(info)}
        return inspect(self.candidates), inspect(self.published)

    def create(self, candidate_id, principal):
        target = self.candidates / candidate_id
        if target.exists() or (self.published / candidate_id).exists():
            raise issue("CONFLICT", "candidate", candidate_id, "candidate already exists")
        require_writable(self.root, scope_id=candidate_id, minimum_bytes=4096)
        target.mkdir(mode=0o700)
        _fsync_dir(self.candidates)
        try:
            db_path = target / "display.sqlite"
            fd = os.open(db_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            db = sqlite3.connect(db_path, isolation_level=None)
            try:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA synchronous=FULL")
                if db.execute("PRAGMA journal_mode=WAL").fetchone() != ("wal",):
                    raise issue("DEPENDENCY_UNAVAILABLE", "candidate", candidate_id, "WAL unavailable")
                db.executescript("BEGIN IMMEDIATE;\n" + _display_schema_ddl.SQL + "\nCOMMIT;")
                checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint != (0, 0, 0):
                    raise issue("INTEGRITY_ERROR", "candidate", candidate_id, "checkpoint incomplete")
            finally:
                db.close()
            _fsync_dir(target)
            directory_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                verified = verify_frozen_database(directory_fd, max_bytes=self.max_bytes,
                                                  deadline=time.monotonic() + 30)
                manifest = {"mode": MODE, "format_version": 2, "candidate_id": candidate_id,
                            "ddl_sha": PINNED_DDL_SHA, "artifact_policy_sha": self.policy_sha,
                            "engine_metadata": {"sqlite_version": sqlite3.sqlite_version,
                                                "journal_mode": "WAL", "synchronous": "FULL"},
                            "candidate_directory_identity": _identity(os.fstat(directory_fd)),
                            "creator_commit": self.creator_commit,
                            "creator_principal_id": principal.name, **verified}
            finally:
                os.close(directory_fd)
            _write_exclusive(target / "manifest.json", manifest)
            return self.verify(self.candidates, candidate_id)
        except OSError as exc:
            raise from_os_error(exc, candidate_id) from exc

    def verify(self, base, candidate_id):
        target = Path(base) / candidate_id
        if set(os.listdir(target)) != {"display.sqlite", "manifest.json"}:
            raise issue("INTEGRITY_ERROR", "candidate", candidate_id, "candidate contents mismatch")
        manifest = read_record(target / "manifest.json")
        fields = {"mode", "format_version", "candidate_id", "ddl_sha", "artifact_policy_sha",
                  "engine_metadata", "candidate_directory_identity", "creator_commit",
                  "creator_principal_id", "db_sha", "db_bytes", "db_identity", "verification"}
        if (set(manifest) != fields or manifest["mode"] != MODE or manifest["format_version"] != 2
                or manifest["candidate_id"] != candidate_id or manifest["ddl_sha"] != PINNED_DDL_SHA
                or manifest["artifact_policy_sha"] != self.policy_sha):
            raise issue("INTEGRITY_ERROR", "candidate", candidate_id, "manifest mismatch")
        directory_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if manifest["candidate_directory_identity"] != _identity(os.fstat(directory_fd)):
                raise issue("INTEGRITY_ERROR", "candidate", candidate_id, "directory identity changed")
            verified = verify_frozen_database(directory_fd, max_bytes=self.max_bytes,
                                              deadline=time.monotonic() + 30)
        finally:
            os.close(directory_fd)
        if any(manifest[key] != value for key, value in verified.items()):
            raise issue("INTEGRITY_ERROR", "candidate", candidate_id, "database mismatch")
        return manifest, digest(manifest)

    def approve(self, params, principal):
        manifest, manifest_sha = self.verify(self.candidates, params["candidate_id"])
        if manifest["creator_principal_id"] == principal.name:
            raise issue("PERMISSION_DENIED", "candidate", params["candidate_id"], "distinct approver required")
        if params["manifest_sha"] != manifest_sha or params["policy_sha"] != self.policy_sha:
            raise issue("CONFLICT", "candidate", params["candidate_id"], "approval evidence mismatch")
        record = {"mode": MODE, "format_version": 2, "approval_id": params["approval_id"],
                  "candidate_id": params["candidate_id"], "target_name": params["candidate_id"],
                  "manifest_sha": manifest_sha, "artifact_policy_sha": self.policy_sha,
                  "scope": "PUBLISH_EMPTY_UNACTIVATED", "approver_reference": params["reference"],
                  "approver_principal_id": principal.name}
        sha = _write_exclusive(self.approvals / (params["approval_id"] + ".json"), record)
        return record, sha

    def verify_approval(self, candidate_id, approval_id, base=None):
        manifest, manifest_sha = self.verify(base or self.candidates, candidate_id)
        record = read_record(self.approvals / (approval_id + ".json"))
        fields = {"mode", "format_version", "approval_id", "candidate_id", "target_name", "manifest_sha", "artifact_policy_sha", "scope", "approver_principal_id", "approver_reference"}
        approver = record.get("approver_principal_id")
        reference = record.get("approver_reference")
        if (set(record) != fields or not isinstance(approver, str) or not approver
                or approver not in self.trusted_approvers
                or not isinstance(reference, str) or not reference.strip()):
            raise issue("INTEGRITY_ERROR", "candidate", candidate_id, "untrusted approval identity or fields")
        expected = {"mode": MODE, "format_version": 2, "approval_id": approval_id,
                    "candidate_id": candidate_id, "target_name": candidate_id,
                    "manifest_sha": manifest_sha, "artifact_policy_sha": self.policy_sha,
                    "scope": "PUBLISH_EMPTY_UNACTIVATED"}
        if any(record.get(key) != value for key, value in expected.items()):
            raise issue("INTEGRITY_ERROR", "approval", approval_id, "approval mismatch")
        if record.get("approver_principal_id") == manifest["creator_principal_id"]:
            raise issue("INTEGRITY_ERROR", "approval", approval_id, "approver is creator")
        return {"manifest_sha": manifest_sha, "approval_id": approval_id,
                "approval_sha": digest(record)}

    def publish(self, candidate_id, approval_id):
        evidence = self.verify_approval(candidate_id, approval_id)
        source, target = self.candidates / candidate_id, self.published / candidate_id
        try:
            _rename_no_replace(source, target)
            _fsync_dir(self.candidates)
            _fsync_dir(self.published)
        except OSError as exc:
            raise from_os_error(exc, candidate_id) from exc
        actual = self.verify_approval(candidate_id, approval_id, self.published)
        if actual != evidence:
            raise issue("INTEGRITY_ERROR", "candidate", candidate_id, "published evidence changed")
        return actual

    def audit_snapshot(self, owner_id):
        """Bind even incomplete audit bytes without repairing or overwriting them."""
        path = self.audits / (owner_id + ".json")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_size > 65536 or before.st_uid not in {0, os.geteuid()}
                    or before.st_mode & 0o022):
                raise issue("INTEGRITY_ERROR", "audit", owner_id, "unsafe original audit")
            raw = os.read(fd, 65537)
            after = os.fstat(fd)
            if (_identity(before) != _identity(after) or before.st_size != len(raw)
                    or before.st_mtime_ns != after.st_mtime_ns
                    or before.st_ctime_ns != after.st_ctime_ns):
                raise issue("INTEGRITY_ERROR", "audit", owner_id, "original audit changed")
        finally:
            os.close(fd)
        try:
            complete = canonical(json.loads(raw)) == raw
        except (ValueError, UnicodeError):
            complete = False
        return {"relative_path": path.name, "sha": hashlib.sha256(raw).hexdigest(),
                "record_state": "COMPLETE" if complete else "PARTIAL"}

    def audit(self, owner_id, request_sha, result, kind="ORIGINAL", relates_to=None):
        record = {"mode": MODE, "owner_id": owner_id, "request_sha": request_sha,
                  "result_sha": digest(result), "kind": kind, "relates_to": relates_to}
        path = self.audits / (owner_id + ".json")
        try:
            sha = _write_exclusive(path, record)
        except FileExistsError:
            if read_record(path) != record:
                raise issue("INTEGRITY_ERROR", "audit", owner_id, "audit conflicts")
            sha = digest(record)
        return record, sha, path.name