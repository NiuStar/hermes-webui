"""Trusted configuration for the isolated application runtime v2 domain."""

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from api.application_operation_issue import issue
from api.application_protocol import MODE, Principal, digest


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    ledger_path: Path
    artifact_root: Path
    artifact_policy: dict
    artifact_policy_sha: str
    creator_commit: str
    principals: dict
    busy_timeout_ms: int = 5000
    artifact_max_bytes: int = 16 * 1024 * 1024
    fixed_tool: dict | None = None
    cli_socket: str | None = None
    cli_broker_uid: int | None = None

    def principal(self, name):
        raw = self.principals.get(name)
        if type(raw) is not dict or set(raw) != {"permissions", "task_read_all"}:
            raise issue("PERMISSION_DENIED", message="principal is not configured")
        permissions = raw["permissions"]
        if (type(permissions) is not list or any(item not in {"create", "approve", "publish", "recover"}
                                                 for item in permissions)
                or type(raw["task_read_all"]) is not bool):
            raise issue("INTEGRITY_ERROR", "configuration", None, "invalid principal configuration")
        return Principal(name, frozenset(permissions), raw["task_read_all"])


def load_config(path):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022
                or info.st_size > 1024 * 1024):
            raise issue("PERMISSION_DENIED", "configuration", None, "untrusted runtime configuration")
        raw = os.read(fd, info.st_size + 1)
    finally:
        os.close(fd)
    try:
        data = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise issue("INVALID_REQUEST", "configuration", None, "invalid runtime configuration") from exc
    required = {"runtime_format", "ledger_path", "artifact_root", "artifact_policy",
                "creator_commit", "principals"}
    optional = {"busy_timeout_ms", "artifact_max_bytes", "quota", "fixed_tool", "cli_socket", "cli_broker_uid"}
    if type(data) is not dict or not required <= set(data) or set(data) - required - optional:
        raise issue("INVALID_REQUEST", "configuration", None, "configuration schema mismatch")
    if data["runtime_format"] != MODE:
        raise issue("UNSUPPORTED_VERSION", "configuration", None, f"only {MODE} is supported")
    quota = data.get("quota", {"enabled": False})
    if type(quota) is not dict or set(quota) != {"enabled"} or type(quota["enabled"]) is not bool:
        raise issue("INVALID_REQUEST", "configuration", None, "invalid quota configuration")
    if quota["enabled"]:
        raise issue("UNSUPPORTED_FEATURE", "configuration", None,
                    "optional quota is not implemented for application_runtime_v2")
    policy = data["artifact_policy"]
    if type(policy) is not dict:
        raise issue("INVALID_REQUEST", "configuration", None, "artifact_policy must be an object")
    busy = data.get("busy_timeout_ms", 5000)
    maximum = data.get("artifact_max_bytes", 16 * 1024 * 1024)
    if type(busy) is not int or not 1 <= busy <= 60000 or type(maximum) is not int or maximum < 4096:
        raise issue("INVALID_REQUEST", "configuration", None, "invalid runtime limits")
    for field in ("ledger_path", "artifact_root"):
        value = data[field]
        if not isinstance(value, str) or not Path(value).is_absolute() or ".." in Path(value).parts:
            raise issue("INVALID_REQUEST", "configuration", field, "absolute storage path required")
    if ("cli_socket" in data) != ("cli_broker_uid" in data):
        raise issue("INVALID_REQUEST", message="CLI socket and broker UID must be configured together")
    if "cli_socket" in data and (type(data["cli_socket"]) is not str
            or not Path(data["cli_socket"]).is_absolute() or ".." in Path(data["cli_socket"]).parts
            or type(data["cli_broker_uid"]) is not int or data["cli_broker_uid"] <= 0):
        raise issue("INVALID_REQUEST", message="invalid CLI broker configuration")
    return RuntimeConfig(Path(data["ledger_path"]), Path(data["artifact_root"]), policy,
                         digest(policy), str(data["creator_commit"]), data["principals"], busy, maximum, data.get("fixed_tool"),
                         data.get("cli_socket"), data.get("cli_broker_uid"))