"""Strict request and principal contracts for application runtime v2."""

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from api.application_operation_issue import issue

MODE = "application_runtime_v2"
OPERATIONS = frozenset({"create", "approve", "publish", "recover"})
HEX = frozenset("0123456789abcdef")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def id32(value, name):
    if type(value) is not str or len(value) != 32 or any(c not in HEX for c in value):
        raise issue("INVALID_REQUEST", message=f"{name} must be 32 lowercase hexadecimal characters")
    return value


def sha256(value, name):
    if type(value) is not str or len(value) != 64 or any(c not in HEX for c in value):
        raise issue("INVALID_REQUEST", message=f"{name} must be a lowercase SHA-256")
    return value


@dataclass(frozen=True, slots=True)
class Principal:
    name: str
    permissions: frozenset[str]
    task_read_all: bool = False

    def require(self, operation):
        if operation not in self.permissions:
            raise issue("PERMISSION_DENIED", message=f"principal cannot {operation}")

    def require_read(self, owner):
        if owner != self.name and not self.task_read_all:
            raise issue("NOT_FOUND", message="task not found")


@dataclass(frozen=True, slots=True)
class Request:
    mode: str
    task_id: str
    operation: str
    parameters: Mapping[str, object]

    @property
    def request_sha(self):
        return digest({"mode": self.mode, "task_id": self.task_id,
                       "operation": self.operation, "parameters": dict(self.parameters)})


def parse_request(raw):
    if type(raw) is bytes:
        if len(raw) > 16384:
            raise issue("INVALID_REQUEST", message="request too large")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise issue("INVALID_REQUEST", message="request must be UTF-8 JSON") from exc
    if type(raw) is str:
        try:
            data = json.loads(raw, object_pairs_hook=_unique)
        except (ValueError, json.JSONDecodeError) as exc:
            raise issue("INVALID_REQUEST", message="request must be valid JSON") from exc
    elif type(raw) is dict:
        data = raw
    else:
        raise issue("INVALID_REQUEST", message="request must be an object")
    if type(data) is not dict or set(data) != {"mode", "task_id", "operation", "parameters"}:
        raise issue("INVALID_REQUEST", message="request schema mismatch")
    if data["mode"] != MODE:
        raise issue("UNSUPPORTED_VERSION", message=f"only {MODE} is supported")
    try:
        if len(canonical(data)) > 16384:
            raise issue("INVALID_REQUEST", message="request too large")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise issue("INVALID_REQUEST", message="request must be JSON-compatible") from exc
    operation = data["operation"]
    if type(operation) is not str or operation not in OPERATIONS:
        raise issue("INVALID_REQUEST", message="unsupported operation")
    shapes = {
        "create": {"candidate_id"},
        "approve": {"candidate_id", "approval_id", "manifest_sha", "policy_sha", "reference"},
        "publish": {"candidate_id", "approval_id"},
        "recover": {"candidate_id", "interrupted_task_id", "decision",
                    "expected_evidence_sha", "approval_id"},
    }
    params = data["parameters"]
    if type(params) is not dict or set(params) != shapes[operation]:
        raise issue("INVALID_REQUEST", message="parameters schema mismatch")
    id32(data["task_id"], "task_id")
    id32(params["candidate_id"], "candidate_id")
    if "approval_id" in params and params["approval_id"] is not None:
        id32(params["approval_id"], "approval_id")
    if operation in {"approve", "publish"}:
        id32(params["approval_id"], "approval_id")
    if operation == "approve":
        sha256(params["manifest_sha"], "manifest_sha")
        sha256(params["policy_sha"], "policy_sha")
        ref = params["reference"]
        if type(ref) is not str or not ref.strip() or len(ref.encode()) > 4096:
            raise issue("INVALID_REQUEST", message="invalid approval reference")
    if operation == "recover":
        id32(params["interrupted_task_id"], "interrupted_task_id")
        sha256(params["expected_evidence_sha"], "expected_evidence_sha")
        if type(params["decision"]) is not str or params["decision"] not in {"close_failed", "finalize_existing", "resume_publish"}:
            raise issue("INVALID_REQUEST", message="invalid recovery decision")
        if (params["decision"] == "close_failed" and params["approval_id"] is not None) or (params["decision"] == "resume_publish" and params["approval_id"] is None):
            raise issue("INVALID_REQUEST", message="recovery approval does not match decision")
    return Request(MODE, data["task_id"], operation, MappingProxyType(dict(params)))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result