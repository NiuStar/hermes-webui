"""Restricted updater sidecar using the mounted Docker Engine socket.

The WebUI process never receives Docker daemon access. A same-image sidecar
accepts one authenticated local action for its fixed target and repository,
then performs pull, health-gated replacement, and rollback if needed.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import copy
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import stat
import sys
import threading
import time
import urllib.parse
from collections import OrderedDict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SOCKET_PATH = "/var/run/docker.sock"
API_PREFIX = "/v1.41"
CONTROL_SOCKET = "/run/hermes-webui-updater/control.sock"
CONTROL_TOKEN = "/run/hermes-webui-updater/token"


class DockerEngineError(RuntimeError):
    pass


def _read_trusted_manifest_file(path: str | os.PathLike[str], limit: int) -> bytes:
    """Read a regular, non-symlink file once; never follow a swapped final link."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or not 0 < info.st_size <= limit:
            raise DockerEngineError("Insecure or invalid Agent manifest file")
        data = os.read(fd, limit + 1)
        if len(data) != info.st_size:
            raise DockerEngineError("Agent manifest changed during read")
        return data
    finally:
        os.close(fd)


def preflight_agent_manifest(
    manifest_path: str | os.PathLike[str], signature_path: str | os.PathLike[str], *,
    repository: str, platform: str, agent_commit: str,
) -> dict[str, str]:
    """Validate publisher-bound data without pulling or replacing a container.

    The public-key file must be provisioned independently of the manifest;
    this function alone is not permission to deploy an Agent image.
    """
    if __package__:
        from .agent_image_manifest import ManifestError, verify_manifest
    else:  # Compose runs this file directly from /apptoo/api.
        from agent_image_manifest import ManifestError, verify_manifest

    key_path = os.getenv("HERMES_WEBUI_AGENT_MANIFEST_PUBKEY")
    key_pin = os.getenv("HERMES_WEBUI_AGENT_MANIFEST_PUBKEY_SHA256", "")
    if not key_path or not re.fullmatch(r"[0-9a-f]{64}", key_pin):
        raise DockerEngineError("Trusted Agent manifest key is not configured")
    try:
        public_key = base64.b64decode(_read_trusted_manifest_file(key_path, 128).strip(), validate=True)
        if not hmac.compare_digest(hashlib.sha256(public_key).hexdigest(), key_pin):
            raise DockerEngineError("Trusted Agent manifest key fingerprint mismatch")
        raw = _read_trusted_manifest_file(manifest_path, 16384)
        signature = _read_trusted_manifest_file(signature_path, 128).decode("ascii").strip()
        manifest = verify_manifest(raw, signature, public_key, repository=repository,
                                   platform=platform, agent_commit=agent_commit)
    except (OSError, UnicodeError, ValueError, binascii.Error, ManifestError) as exc:
        raise DockerEngineError("Agent image manifest could not be authenticated") from exc
    return {
        "image": manifest["repository"] + "@" + manifest["digest"],
        "webui_commit": manifest["webui_commit"],
        "agent_commit": manifest["agent_commit"],
        "webui_version": manifest["webui_version"],
        "agent_version": manifest["agent_version"],
        "platform": manifest["platform"],
    }


class UpdateProgress:
    """Thread-safe state for recent single-flight Docker updates."""

    TOTAL_STEPS = 7
    MAX_OPERATIONS = 8

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._operations: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def start(self, version: str) -> tuple[str, dict[str, Any]]:
        operation_id = secrets.token_hex(16)
        now = time.time()
        with self._lock:
            self._operations[operation_id] = {
                "operation_id": operation_id,
                "version": version,
                "state": "running",
                "stage": "accepted",
                "step": 0,
                "total_steps": self.TOTAL_STEPS,
                "started_at": now,
                "updated_at": now,
                "rolled_back": False,
            }
            while len(self._operations) > self.MAX_OPERATIONS:
                self._operations.popitem(last=False)
            return operation_id, self._snapshot_locked(operation_id)

    def advance(self, operation_id: str, stage: str, step: int, **extra: Any) -> None:
        with self._lock:
            if not self._matches(operation_id):
                return
            operation = self._operations[operation_id]
            operation.update({"stage": stage, "step": step, "updated_at": time.time()})
            operation.update(extra)

    def complete(self, operation_id: str, *, cleanup_pending: bool = False) -> None:
        with self._lock:
            if not self._matches(operation_id):
                return
            self._operations[operation_id].update({
                "state": "succeeded",
                "stage": "completed",
                "step": self.TOTAL_STEPS,
                "updated_at": time.time(),
                "cleanup_pending": cleanup_pending,
            })

    def fail(self, operation_id: str) -> None:
        with self._lock:
            if not self._matches(operation_id):
                return
            operation = self._operations[operation_id]
            if operation.get("state") != "failed":
                failed_stage = operation.get("stage")
                operation.update({
                    "state": "failed",
                    "failed_stage": failed_stage,
                    "old_container_untouched": failed_stage in {
                        "accepted", "pulling_image", "verifying_image",
                    },
                    "updated_at": time.time(),
                })

    def snapshot(self, operation_id: str) -> dict[str, Any] | None:
        with self._lock:
            if not self._matches(operation_id):
                return None
            return self._snapshot_locked(operation_id)

    def _matches(self, operation_id: str) -> bool:
        return operation_id in self._operations

    def _snapshot_locked(self, operation_id: str) -> dict[str, Any]:
        result = copy.deepcopy(self._operations[operation_id])
        started_at = float(result.get("started_at") or time.time())
        result["elapsed_seconds"] = max(0, int(time.time() - started_at))
        return result


class DockerEngine:
    def __init__(self, socket_path: str = SOCKET_PATH):
        self.socket_path = socket_path

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, *, timeout: int = 30, discard_body: bool = False) -> Any:
        if not path.startswith(API_PREFIX + "/"):
            path = API_PREFIX + path
        payload = json.dumps(body, separators=(",", ":")).encode() if body is not None else b""
        request = (
            f"{method} {path} HTTP/1.1\r\n"
            "Host: docker\r\n"
            "Connection: close\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n\r\n"
        ).encode() + payload
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(self.socket_path)
            sock.sendall(request)
            chunks = []
            total = 0
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                total += len(chunk)
                if not discard_body and total > 8 * 1024 * 1024:
                    raise DockerEngineError("Docker API response exceeded 8 MiB")
                if not discard_body or total <= 65536:
                    chunks.append(chunk)
        finally:
            sock.close()
        raw = b"".join(chunks)
        head, _, data = raw.partition(b"\r\n\r\n")
        status_line = head.splitlines()[0].decode("latin1") if head else ""
        try:
            status = int(status_line.split()[1])
        except (IndexError, ValueError):
            raise DockerEngineError("Docker socket returned an invalid HTTP response")
        headers = {line.split(b":", 1)[0].lower(): line.split(b":", 1)[1].strip().lower() for line in head.splitlines()[1:] if b":" in line}
        if headers.get(b"transfer-encoding") == b"chunked":
            data = _decode_chunked(data)
        if status >= 300:
            detail = data.decode("utf-8", "replace")[:300]
            raise DockerEngineError(f"Docker API {status}: {detail}")
        if discard_body or not data:
            return None
        try:
            return json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            return data.decode("utf-8", "replace")

    def inspect(self, container: str) -> dict[str, Any]:
        return self.request("GET", "/containers/" + urllib.parse.quote(container, safe="") + "/json")

    def create(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/containers/create?name=" + urllib.parse.quote(name, safe=""), payload)

    def start(self, container: str) -> None:
        self.request("POST", f"/containers/{urllib.parse.quote(container, safe='')}/start")

    def stop(self, container: str) -> None:
        self.request("POST", f"/containers/{urllib.parse.quote(container, safe='')}/stop?t=20")

    def remove(self, container: str, *, force: bool = False) -> None:
        suffix = "?force=1" if force else ""
        self.request("DELETE", f"/containers/{urllib.parse.quote(container, safe='')}{suffix}")

    def rename(self, container: str, name: str) -> None:
        self.request("POST", f"/containers/{urllib.parse.quote(container, safe='')}/rename?name={urllib.parse.quote(name, safe='')}")

    def pull(self, image: str) -> None:
        # The daemon owns registry credentials and network access. The response
        # stream is intentionally drained without returning registry details.
        self.request("POST", "/images/create?fromImage=" + urllib.parse.quote(image, safe=""), timeout=600, discard_body=True)

    def inspect_image(self, image: str) -> dict[str, Any]:
        return self.request("GET", "/images/" + urllib.parse.quote(image, safe="") + "/json")


def _decode_chunked(data: bytes) -> bytes:
    out = bytearray()
    while data:
        line, sep, rest = data.partition(b"\r\n")
        if not sep:
            break
        size = int(line.split(b";", 1)[0], 16)
        if size == 0:
            break
        out.extend(rest[:size])
        data = rest[size + 2:]
    return bytes(out)


def _container_name(info: dict[str, Any]) -> str:
    names = info.get("Name") or ""
    return names.lstrip("/") or os.environ.get("HERMES_WEBUI_CONTAINER_NAME", "hermes-webui")


def _require_free_transaction_names(engine: DockerEngine, *names: str) -> None:
    """Never take over a stopped backup or another update's temporary owner."""
    for name in names:
        try:
            existing = engine.inspect(name)
        except DockerEngineError as exc:
            if re.search(r'(?:\b404\b|not found|no such container)', str(exc), re.I):
                continue
            raise
        if existing:
            raise DockerEngineError("Existing update transaction container blocks replacement")


_BAKED_AGENT_REVISION_LABEL = "org.opencontainers.image.hermes-agent.revision"
_BAKED_AGENT_PATH_LABEL = "org.opencontainers.image.hermes-agent.path"
_BAKED_AGENT_PATH = "/opt/hermes"
_LEGACY_AGENT_DIRS = {
    "/home/hermeswebui/.hermes/hermes-agent",
    "/opt/hermes-agent",
}
_BIND_OPTION_NAMES = {
    "ro", "rw", "z", "Z", "shared", "rshared", "slave", "rslave",
    "private", "rprivate", "nocopy",
}
_PRESERVED_HOST_CONFIG_KEYS = (
    "Binds", "Mounts", "PortBindings", "RestartPolicy", "LogConfig",
    "NetworkMode", "ExtraHosts", "Dns", "DnsSearch", "DnsOptions",
    "CapAdd", "CapDrop", "SecurityOpt", "ReadonlyRootfs", "Init",
    "PidsLimit", "ShmSize", "Memory", "NanoCpus", "CpuShares",
    "Devices", "DeviceRequests", "GroupAdd", "Privileged", "UsernsMode",
    "Tmpfs", "IpcMode", "PidMode", "UTSMode", "CgroupnsMode", "Runtime",
    "MaskedPaths", "ReadonlyPaths", "Ulimits", "Sysctls", "CpuPeriod",
    "CpuQuota", "CpuRealtimePeriod", "CpuRealtimeRuntime", "CpusetCpus",
    "CpusetMems", "MemoryReservation", "MemorySwap", "MemorySwappiness",
    "OomKillDisable", "OomScoreAdj", "BlkioWeight",
)


def _baked_agent_contract(image_info: dict[str, Any] | None) -> tuple[str, str] | None:
    labels = ((image_info or {}).get("Config") or {}).get("Labels") or {}
    revision = str(labels.get(_BAKED_AGENT_REVISION_LABEL) or "").strip()
    path = str(labels.get(_BAKED_AGENT_PATH_LABEL) or "").strip()
    if path != _BAKED_AGENT_PATH or not re.fullmatch(r"[0-9a-f]{40}", revision):
        return None
    return path, revision


def _env_value(env: list[Any], name: str) -> str | None:
    prefix = name + "="
    for item in env:
        text = str(item)
        if text.startswith(prefix):
            return text[len(prefix):]
    return None


def _set_env_value(env: list[Any], name: str, value: str) -> list[str]:
    prefix = name + "="
    result = [str(item) for item in env if not str(item).startswith(prefix)]
    result.append(prefix + value)
    return result


def _migrated_pythonpath(value: str | None, baked_path: str) -> str:
    if value is None:
        return baked_path
    paths = value.split(":")
    replaced = False
    for index, path in enumerate(paths):
        if path in _LEGACY_AGENT_DIRS:
            paths[index] = baked_path
            replaced = True
    if not replaced and baked_path not in paths:
        paths.insert(0, baked_path)
    return ":".join(paths)


def _bind_destination(spec: Any) -> str:
    parts = str(spec).rsplit(":", 2)
    options = parts[-1].split(",") if len(parts) == 3 else []
    if options and all(option in _BIND_OPTION_NAMES for option in options):
        return parts[-2]
    return parts[-1] if len(parts) >= 2 else ""


def _create_payload(
    info: dict[str, Any],
    image: str,
    *,
    image_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = copy.deepcopy(info.get("Config") or {})
    host = copy.deepcopy(info.get("HostConfig") or {})
    config["Image"] = image
    config.pop("Hostname", None)
    host = {
        key: value
        for key, value in host.items()
        if key in _PRESERVED_HOST_CONFIG_KEYS and value not in (None, {}, [])
    }
    image_labels = ((image_info or {}).get("Config") or {}).get("Labels") or {}
    if image_labels:
        labels = dict(config.get("Labels") or {})
        for key, value in image_labels.items():
            if str(key).startswith("org.opencontainers.image."):
                labels[str(key)] = value
        config["Labels"] = labels

    baked_agent = _baked_agent_contract(image_info)
    env = list(config.get("Env") or [])
    configured_agent = _env_value(env, "HERMES_WEBUI_AGENT_DIR")
    binds = list(host.get("Binds") or [])
    mounts = list(host.get("Mounts") or [])
    has_legacy_agent_mount = any(
        _bind_destination(spec) in _LEGACY_AGENT_DIRS for spec in binds
    ) or any(
        str((mount or {}).get("Target") or (mount or {}).get("Destination") or "")
        in _LEGACY_AGENT_DIRS
        for mount in mounts
    )
    standard_override = configured_agent in _LEGACY_AGENT_DIRS or (
        configured_agent is None and has_legacy_agent_mount
    )
    if baked_agent is not None and standard_override:
        env = _set_env_value(env, "HERMES_WEBUI_AGENT_DIR", baked_agent[0])
        env = _set_env_value(
            env,
            "PYTHONPATH",
            _migrated_pythonpath(_env_value(env, "PYTHONPATH"), baked_agent[0]),
        )
        config["Env"] = env
        filtered_binds = [
            spec for spec in binds
            if _bind_destination(spec) not in _LEGACY_AGENT_DIRS
        ]
        if filtered_binds:
            host["Binds"] = filtered_binds
        else:
            host.pop("Binds", None)
        filtered_mounts = [
            mount for mount in mounts
            if str((mount or {}).get("Target") or (mount or {}).get("Destination") or "")
            not in _LEGACY_AGENT_DIRS
        ]
        if filtered_mounts:
            host["Mounts"] = filtered_mounts
        else:
            host.pop("Mounts", None)
    payload: dict[str, Any] = config
    payload["HostConfig"] = host
    networks = (info.get("NetworkSettings") or {}).get("Networks") or {}
    container_id = str(info.get("Id") or "")
    generated_aliases = {container_id, container_id[:12]} - {""}
    if networks:
        payload["NetworkingConfig"] = {
            "EndpointsConfig": {
                name: {
                    "Aliases": [
                        alias for alias in (value.get("Aliases") or [])
                        if str(alias) not in generated_aliases
                    ]
                }
                for name, value in networks.items()
            }
        }
    return payload


def _healthy(engine: DockerEngine, name: str) -> bool:
    state = engine.inspect(name).get("State") or {}
    health = state.get("Health")
    if isinstance(health, dict) and health.get("Status"):
        return health.get("Status") == "healthy"
    return False


def _normalized_networks(info: dict[str, Any]) -> dict[str, list[str]]:
    container_id = str(info.get("Id") or "")
    ignored = {container_id, container_id[:12]} - {""}
    networks = (info.get("NetworkSettings") or {}).get("Networks") or {}
    return {
        str(name): sorted(
            str(alias) for alias in (value.get("Aliases") or [])
            if alias and str(alias) not in ignored
        )
        for name, value in networks.items()
    }


def _normalize_runtime_value(value: Any) -> Any:
    """Normalize Docker's equivalent empty/default inspect values."""
    if value in (None, {}, [], "", 0, False):
        return None
    if isinstance(value, dict):
        normalized = {
            str(key): _normalize_runtime_value(item)
            for key, item in value.items()
        }
        return {key: item for key, item in sorted(normalized.items()) if item is not None} or None
    if isinstance(value, list):
        normalized = [_normalize_runtime_value(item) for item in value]
        return [item for item in normalized if item is not None] or None
    return value


def _verify_runtime_contract(
    old: dict[str, Any],
    new: dict[str, Any],
    *,
    expected_payload: dict[str, Any] | None = None,
) -> None:

    old_host = (
        expected_payload.get("HostConfig") or {}
        if expected_payload is not None
        else old.get("HostConfig") or {}
    )
    new_host = new.get("HostConfig") or {}
    mismatches = [
        key
        for key in _PRESERVED_HOST_CONFIG_KEYS
        if _normalize_runtime_value(old_host.get(key))
        != _normalize_runtime_value(new_host.get(key))
    ]
    if _normalized_networks(old) != _normalized_networks(new):
        mismatches.append("Networks")
    if expected_payload is not None:
        expected_env = [str(item) for item in (expected_payload.get("Env") or [])]
        actual_env = [str(item) for item in ((new.get("Config") or {}).get("Env") or [])]
        if expected_env != actual_env:
            mismatches.append("Env")
        expected_labels = expected_payload.get("Labels") or {}
        actual_labels = (new.get("Config") or {}).get("Labels") or {}
        expected_identity = {
            str(key): value for key, value in expected_labels.items()
            if str(key).startswith("org.opencontainers.image.")
        }
        actual_identity = {
            str(key): value for key, value in actual_labels.items()
            if str(key).startswith("org.opencontainers.image.")
        }
        if actual_identity != expected_identity:
            mismatches.append("ImageLabels")
    if mismatches:
        raise DockerEngineError(
            "replacement runtime contract mismatch: " + ", ".join(mismatches)
        )


_AGENT_RUNTIME_PROBE = """
import hashlib
import importlib.util
import pathlib
import sys

revision, baked_arg, staged_arg = sys.argv[1:]
baked, staged = pathlib.Path(baked_arg), pathlib.Path(staged_arg)
if not all((root / '.hermes-agent-revision').read_text().strip() == revision
           for root in (baked, staged)):
    raise SystemExit(2)

def python_sources(root):
    result = {}
    for path in root.rglob('*.py'):
        relative = path.relative_to(root)
        if any(part == '__pycache__' or part.endswith('.egg-info')
               or part in ('build', 'dist', '.git', '.playwright') for part in relative.parts):
            continue
        if not path.is_file() or path.is_symlink():
            raise SystemExit(2)
        result[str(relative)] = hashlib.sha256(path.read_bytes()).digest()
    return result

baked_sources = python_sources(baked)
if not baked_sources or baked_sources != python_sources(staged):
    raise SystemExit(2)
spec = importlib.util.find_spec('hermes_cli')
if spec is None or spec.origin is None:
    raise SystemExit(2)
origin = pathlib.Path(spec.origin).resolve(strict=True)
if origin not in ((baked / 'hermes_cli' / '__init__.py').resolve(strict=True),
                  (staged / 'hermes_cli' / '__init__.py').resolve(strict=True)):
    raise SystemExit(2)
import hermes_cli
if pathlib.Path(hermes_cli.__file__).resolve(strict=True) != origin:
    raise SystemExit(2)
config_spec = importlib.util.find_spec('hermes_cli.config')
if config_spec is None or config_spec.origin is None:
    raise SystemExit(2)
config_path = pathlib.Path(config_spec.origin).resolve(strict=True)
if config_path != (origin.parent / 'config.py').resolve(strict=True):
    raise SystemExit(2)
"""

def _verify_imported_agent(engine: DockerEngine, container: str, revision: str) -> None:
    """Probe the replacement's actual venv and staged Agent before cleanup."""
    path = urllib.parse.quote(container, safe="")
    command = ["/app/venv/bin/python3", "-c", _AGENT_RUNTIME_PROBE,
               revision, _BAKED_AGENT_PATH, "/app/hermes-agent-src"]
    created = engine.request("POST", f"/containers/{path}/exec", {
        "AttachStdout": False, "AttachStderr": False, "Tty": False,
        "User": "hermeswebui", "WorkingDir": "/app", "Cmd": command,
    })
    exec_id = (created or {}).get("Id")
    if not isinstance(exec_id, str) or not re.fullmatch(r"[0-9a-f]{64}", exec_id):
        raise DockerEngineError("Could not start runtime Agent verification")
    engine.request("POST", f"/exec/{exec_id}/start", {"Detach": True, "Tty": False})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        state = engine.request("GET", f"/exec/{exec_id}/json")
        if not isinstance(state, dict):
            break
        if state.get("Running") is False:
            if type(state.get("ExitCode")) is int and state["ExitCode"] == 0:
                return
            break
        time.sleep(0.25)
    raise DockerEngineError("Replacement runtime Agent identity could not be verified")


def _start_if_stopped(engine: DockerEngine, name: str) -> None:
    state = engine.inspect(name).get("State") or {}
    if not state.get("Running"):
        engine.start(name)


def _wait_for_restored_health(engine: DockerEngine, name: str, timeout: int) -> None:
    """Rollback is not complete until the original service is healthy again."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _healthy(engine, name):
            return
        time.sleep(2)
    raise DockerEngineError("restored owner did not become healthy")


def _verified_image_id(
    engine: DockerEngine,
    image: str,
    version: str,
    *,
    inspected: dict[str, Any] | None = None,
) -> str:
    inspected = inspected if inspected is not None else engine.inspect_image(image)
    labels = (inspected.get("Config") or {}).get("Labels") or {}
    actual = str(labels.get("org.opencontainers.image.version") or "").strip()
    expected = version.removeprefix("exp-").removeprefix("v")
    if actual not in {version, expected}:
        raise DockerEngineError("pulled image version does not match the verified GitHub release")
    image_id = str(inspected.get("Id") or "").strip()
    if not image_id.startswith("sha256:"):
        raise DockerEngineError("pulled image has no immutable image ID")
    return image_id


def _verified_agent_image_id(image_info: dict[str, Any], expected: dict[str, str]) -> str:
    """Bind an inspected image to independently authenticated manifest fields."""
    image = expected.get("image") or ""
    if not re.fullmatch(r"[a-z0-9._:/-]+@sha256:[0-9a-f]{64}", image):
        raise DockerEngineError("No authenticated Agent image digest")
    digests = image_info.get("RepoDigests") or []
    if image not in digests:
        raise DockerEngineError("Pulled image digest does not match Agent manifest")
    platform = str(image_info.get("Os") or "") + "/" + str(image_info.get("Architecture") or "")
    if platform != expected.get("platform"):
        raise DockerEngineError("Pulled image platform does not match Agent manifest")
    labels = (image_info.get("Config") or {}).get("Labels") or {}
    if any(labels.get(label) != expected.get(field) for label, field in (
        ("org.opencontainers.image.version", "webui_version"),
        ("org.opencontainers.image.revision", "webui_commit"),
        (_BAKED_AGENT_REVISION_LABEL, "agent_commit"),
    )) or labels.get(_BAKED_AGENT_PATH_LABEL) != _BAKED_AGENT_PATH:
        raise DockerEngineError("Pulled image identities do not match Agent manifest")
    image_id = image_info.get("Id")
    if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise DockerEngineError("Pulled Agent image has no valid immutable image ID")
    return image_id


def _require_unmounted_baked_agent(container: dict[str, Any]) -> None:
    """A signed image cannot upgrade an overridden runtime Agent."""
    env = list((container.get("Config") or {}).get("Env") or [])
    agent_paths = [str(item).partition("=")[2] for item in env
                   if str(item).startswith("HERMES_WEBUI_AGENT_DIR=")]
    python_paths = [str(item).partition("=")[2] for item in env
                    if str(item).startswith("PYTHONPATH=")]
    if (len(agent_paths) > 1 or agent_paths and agent_paths[0] != _BAKED_AGENT_PATH
            or len(python_paths) > 1 or python_paths and python_paths[0] != _BAKED_AGENT_PATH):
        raise DockerEngineError("Runtime Agent source differs from baked image")
    host = container.get("HostConfig") or {}
    destinations = [_bind_destination(spec) for spec in (host.get("Binds") or [])]
    tmpfs = host.get("Tmpfs") or {}
    if not isinstance(tmpfs, dict):
        raise DockerEngineError("Runtime tmpfs mount topology is invalid")
    destinations.extend(tmpfs)
    for mount in (host.get("Mounts") or []) + (container.get("Mounts") or []):
        destinations.append(str(mount.get("Target") or mount.get("Destination") or ""))
    for destination in destinations:
        path = os.path.normpath(destination)
        if (path == _BAKED_AGENT_PATH or path.startswith(_BAKED_AGENT_PATH + "/")
                or _BAKED_AGENT_PATH.startswith(path.rstrip("/") + "/")
                or path in ({"/app", "/app/hermes-agent-src"} | _LEGACY_AGENT_DIRS)
                or path.startswith("/app/hermes-agent-src/")):
            raise DockerEngineError("Runtime mount obscures baked Hermes Agent")


def _require_unmanaged_agent_target(container: dict[str, Any]) -> None:
    """The socket transaction cannot pin Compose's next recreation digest."""
    labels = (container.get("Config") or {}).get("Labels") or {}
    if not isinstance(labels, dict):
        raise DockerEngineError("Agent target ownership is unknown")
    if any(str(key).startswith("com.docker.compose.") for key in labels):
        raise DockerEngineError("Compose-managed Agent update needs digest reconciliation")


def replace_container(
    target: str,
    image: str,
    version: str | None = None,
    *,
    timeout: int = 180,
    progress: Any = None,
    expected_agent_image: dict[str, str] | None = None,
    expected_old_id: str | None = None,
) -> dict[str, Any]:
    current_stage = "accepted"

    def report(stage: str, step: int, **extra: Any) -> None:
        nonlocal current_stage
        current_stage = stage
        if progress is not None:
            progress(stage, step, **extra)

    engine = DockerEngine()
    old = engine.inspect(target)
    if expected_old_id is not None and old.get("Id") != expected_old_id:
        raise DockerEngineError("Agent update target owner changed after preflight")
    was_running = bool((old.get("State") or {}).get("Running"))
    if not was_running:
        raise DockerEngineError("target container is not running")
    name = _container_name(old)
    backup = f"{name}.hermes-update-old"
    temp = f"{name}.hermes-update-new"
    if expected_agent_image is not None:
        signed_old_id = old.get("Id")
        if not isinstance(signed_old_id, str) or not re.fullmatch(r"[0-9a-f]{64}", signed_old_id):
            raise DockerEngineError("Cannot establish current Agent container ownership")
        _require_unmanaged_agent_target(old)
    report("pulling_image", 1)
    engine.pull(image)
    create_image = image
    report("verifying_image", 2)
    image_info = engine.inspect_image(image)
    if expected_agent_image is not None:
        if image != expected_agent_image.get("image"):
            raise DockerEngineError("Requested image differs from authenticated Agent image")
        create_image = _verified_agent_image_id(image_info, expected_agent_image)
        _require_unmounted_baked_agent(old)
    elif version:
        create_image = _verified_image_id(engine, image, version, inspected=image_info)
        # A WebUI-only update must not silently change the running Agent.
        # Resolve the old container's exact image ID, not its mutable tag.
        old_image_id = old.get("Image")
        if not isinstance(old_image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", old_image_id):
            raise DockerEngineError("Cannot verify current Agent image identity")
        old_image = engine.inspect_image(old_image_id)
        if old_image.get("Id") != old_image_id:
            raise DockerEngineError("Cannot verify current Agent image identity")
        old_contract = _baked_agent_contract(old_image)
        new_contract = _baked_agent_contract(image_info)
        if old_contract is None or new_contract is None or old_contract != new_contract:
            raise DockerEngineError("WebUI-only update would change an unverified Agent revision")
        _require_unmounted_baked_agent(old)
    if _baked_agent_contract(image_info) is None:
        raise DockerEngineError("pulled image has no valid baked Hermes Agent identity")
    create_payload = _create_payload(old, create_image, image_info=image_info)
    if expected_agent_image is not None:
        _require_unmounted_baked_agent(create_payload)
        _require_free_transaction_names(engine, backup, temp)
        current = engine.inspect(target)
        if current.get("Id") != signed_old_id or not (current.get("State") or {}).get("Running"):
            raise DockerEngineError("Agent update target owner changed before stop")
    report("stopping_old_container", 3)
    try:
        engine.rename(signed_old_id if expected_agent_image is not None else target, backup)
    except Exception:
        # Docker may have completed the rename before the socket reply failed.
        # Restore only the exact old ID when the original name is absent.
        old_id = old.get("Id")
        try:
            displaced = engine.inspect(backup)
            if (not isinstance(old_id, str) or displaced.get("Id") != old_id):
                raise DockerEngineError("rename ownership is ambiguous; manual recovery required")
            try:
                engine.inspect(name)
            except DockerEngineError as check_error:
                if not re.search(r'(?:\b404\b|not found|no such container)', str(check_error), re.I):
                    raise
            else:
                raise DockerEngineError("original name was taken; manual recovery required")
            engine.rename(old_id, name)
            restored = engine.inspect(name)
            if restored.get("Id") != old_id or not (restored.get("State") or {}).get("Running"):
                raise DockerEngineError("rename recovery could not verify original owner")
        except DockerEngineError:
            raise
        raise
    created_id: str | None = None
    try:
        engine.stop(signed_old_id if expected_agent_image is not None else backup)
        report("starting_new_container", 4)
        created = engine.create(temp, create_payload)
        if expected_agent_image is not None:
            created_id = (created or {}).get("Id")
            if not isinstance(created_id, str) or not re.fullmatch(r"[0-9a-f]{64}", created_id):
                raise DockerEngineError("Cannot establish new container ownership")
        engine.rename(created_id if expected_agent_image is not None else temp, name)
        engine.start(created_id if expected_agent_image is not None else name)
        report("waiting_for_health", 5)
        deadline = time.monotonic() + timeout
        became_healthy = False
        while time.monotonic() < deadline:
            if _healthy(engine, created_id if expected_agent_image is not None else name):
                became_healthy = True
                break
            time.sleep(2)
        if not became_healthy:
            raise DockerEngineError("replacement container did not become healthy")
        report("verifying_runtime", 6)
        _verify_runtime_contract(old, engine.inspect(created_id if expected_agent_image is not None else name), expected_payload=create_payload)
        if expected_agent_image is not None:
            _verify_imported_agent(engine, created_id, expected_agent_image["agent_commit"])
            if engine.inspect(name).get("Id") != created_id:
                raise DockerEngineError("Agent replacement name owner changed; manual recovery required")
    except Exception:
        failed_stage = current_stage
        report("rolling_back", 0, failed_stage=failed_stage)
        try:
            if expected_agent_image is not None:
                if engine.inspect(backup).get("Id") != old.get("Id"):
                    raise DockerEngineError("rollback owner changed; manual recovery required")
                for possible in (name, temp):
                    try:
                        observed = engine.inspect(possible)
                    except DockerEngineError as exc:
                        if re.search(r'(?:\b404\b|not found|no such container)', str(exc), re.I):
                            continue
                        raise
                    if not created_id or observed.get("Id") != created_id:
                        raise DockerEngineError("rollback name owned by another container; manual recovery required")
                    engine.remove(created_id, force=True)
            else:
                for possible in (name, temp):
                    try:
                        engine.remove(possible, force=True)
                    except Exception:
                        pass
            engine.rename(signed_old_id if expected_agent_image is not None else backup, name)
            if was_running:
                restore_target = signed_old_id if expected_agent_image is not None else name
                _start_if_stopped(engine, restore_target)
                _wait_for_restored_health(engine, restore_target, timeout)
            if expected_agent_image is not None and engine.inspect(name).get("Id") != old.get("Id"):
                raise DockerEngineError("rollback restored another container; manual recovery required")
            report(
                "rolled_back", 0, state="failed", rolled_back=True,
                failed_stage=failed_stage,
            )
        except Exception as rollback_error:
            report(
                "rollback_failed", 0, state="failed", rolled_back=False,
                failed_stage=failed_stage,
            )
            raise DockerEngineError(f"update failed and rollback failed: {rollback_error}")
        raise
    report("cleaning_up", 7)
    if expected_agent_image is not None:
        # Keep the stopped exact previous owner until a separate application
        # acceptance and Compose digest reconciliation are complete.
        return {"ok": True, "container": name, "image": image,
                "rollback_container": backup, "cleanup_pending": True}
    cleanup_pending = False
    try:
        engine.remove(backup, force=True)
    except Exception:
        cleanup_pending = True
    return {"ok": True, "container": name, "image": image, "cleanup_pending": cleanup_pending}


def request_update(channel: str, version: str, sha: str, socket_path: str = CONTROL_SOCKET) -> dict[str, Any]:
    token_path = Path(os.getenv("HERMES_WEBUI_UPDATE_TOKEN_FILE", CONTROL_TOKEN))
    token = token_path.read_text(encoding="utf-8").strip()
    payload = json.dumps({"action": "update", "channel": channel, "version": version, "sha": sha, "token": token}).encode()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    try:
        client.connect(socket_path)
        client.sendall(payload + b"\n")
        response = client.recv(8192)
    finally:
        client.close()
    result = json.loads(response.decode("utf-8"))
    return result if isinstance(result, dict) else {"ok": False, "message": "Invalid updater response"}


def request_agent_update(socket_path: str = CONTROL_SOCKET) -> dict[str, Any]:
    """Request the sidecar's independently configured, signed Agent release."""
    token_path = Path(os.getenv("HERMES_WEBUI_UPDATE_TOKEN_FILE", CONTROL_TOKEN))
    token = token_path.read_text(encoding="utf-8").strip()
    payload = json.dumps({"action": "update_agent", "token": token}).encode()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    try:
        client.connect(socket_path)
        client.sendall(payload + b"\n")
        response = client.recv(8192)
    finally:
        client.close()
    result = json.loads(response.decode("utf-8"))
    return result if isinstance(result, dict) else {"ok": False, "message": "Invalid updater response"}


def request_agent_preflight(socket_path: str = CONTROL_SOCKET) -> dict[str, Any]:
    """Read the sidecar's authenticated availability, without Docker writes."""
    token_path = Path(os.getenv("HERMES_WEBUI_UPDATE_TOKEN_FILE", CONTROL_TOKEN))
    token = token_path.read_text(encoding="utf-8").strip()
    payload = json.dumps({"action": "agent_preflight", "token": token}).encode()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    try:
        client.connect(socket_path)
        client.sendall(payload + b"\n")
        response = client.recv(8192)
    finally:
        client.close()
    result = json.loads(response.decode("utf-8"))
    return result if isinstance(result, dict) else {"ok": False}


def request_health(socket_path: str = CONTROL_SOCKET) -> dict[str, Any]:
    """Probe the authenticated control loop without touching Docker state."""
    token_path = Path(os.getenv("HERMES_WEBUI_UPDATE_TOKEN_FILE", CONTROL_TOKEN))
    token = token_path.read_text(encoding="utf-8").strip()
    payload = json.dumps({"action": "ping", "token": token}).encode()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    try:
        client.connect(socket_path)
        client.sendall(payload + b"\n")
        response = client.recv(8192)
    finally:
        client.close()
    result = json.loads(response.decode("utf-8"))
    return result if isinstance(result, dict) else {"ok": False, "message": "Invalid updater response"}


def request_status(operation_id: str, socket_path: str = CONTROL_SOCKET) -> dict[str, Any]:
    """Read one authenticated update operation without exposing updater config."""
    token_path = Path(os.getenv("HERMES_WEBUI_UPDATE_TOKEN_FILE", CONTROL_TOKEN))
    token = token_path.read_text(encoding="utf-8").strip()
    payload = json.dumps({
        "action": "status",
        "operation_id": operation_id,
        "token": token,
    }).encode()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    try:
        client.connect(socket_path)
        client.sendall(payload + b"\n")
        response = client.recv(8192)
    finally:
        client.close()
    result = json.loads(response.decode("utf-8"))
    return result if isinstance(result, dict) else {"ok": False, "message": "Invalid updater response"}


def _release_is_valid(channel: str, version: str) -> bool:
    if len(version) > 128:
        return False
    if channel == "stable":
        return bool(__import__("re").fullmatch(r"v[0-9][0-9A-Za-z_.-]*", version))
    return channel == "experimental" and bool(__import__("re").fullmatch(r"exp-v[0-9][0-9A-Za-z_.-]*", version))


def _control_request(
    payload: dict[str, Any],
    *,
    busy: threading.Lock,
    expected_token: str,
    progress: UpdateProgress | None = None,
) -> tuple[dict[str, Any], threading.Thread | None]:
    channel = str(payload.get("channel") or "")
    version = str(payload.get("version") or "")
    sha = str(payload.get("sha") or "")
    supplied_token = str(payload.get("token") or "")
    if os.getenv("HERMES_WEBUI_DOCKER_SELF_UPDATE", "").strip() != "1":
        return {"ok": False, "message": "Docker self-update is disabled"}, None
    if not expected_token or not __import__("hmac").compare_digest(supplied_token, expected_token):
        return {"ok": False, "message": "Invalid update request"}, None
    if payload.get("action") == "ping":
        return {"ok": True, "status": "ready", "busy": busy.locked()}, None
    progress_store = progress or UpdateProgress()
    if payload.get("action") == "status":
        operation_id = str(payload.get("operation_id") or "")
        snapshot = progress_store.snapshot(operation_id)
        if snapshot is None:
            return {"ok": False, "message": "Update operation not found"}, None
        return {"ok": True, "progress": snapshot}, None
    if payload.get("action") in ("agent_preflight", "update_agent"):
        manifest_path = os.getenv("HERMES_WEBUI_AGENT_MANIFEST_PATH", "")
        signature_path = os.getenv("HERMES_WEBUI_AGENT_MANIFEST_SIGNATURE_PATH", "")
        agent_commit = os.getenv("HERMES_WEBUI_AGENT_COMMIT", "")
        repository = os.getenv("HERMES_WEBUI_DOCKER_IMAGE", "24802117/hermes-webui").strip()
        if (not manifest_path or not signature_path
                or not re.fullmatch(r"[0-9a-f]{40}", agent_commit)
                or not re.fullmatch(r"(?:[a-z0-9][a-z0-9._-]*(?::[0-9]{1,5})?/)(?:[a-z0-9][a-z0-9._-]*/)*[a-z0-9][a-z0-9._-]*", repository)):
            return {"ok": False, "message": "Signed Agent release is not configured"}, None
        if payload.get("action") == "update_agent" and not busy.acquire(blocking=False):
            return {"ok": False, "message": "Docker update already in progress"}, None
        try:
            expected = preflight_agent_manifest(
                manifest_path, signature_path, repository=repository,
                platform=f"linux/{os.uname().machine.replace('x86_64', 'amd64').replace('aarch64', 'arm64')}",
                agent_commit=agent_commit,
            )
            target = os.getenv("HERMES_WEBUI_UPDATE_TARGET", "hermes-webui").strip()
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", target):
                raise DockerEngineError("Invalid Agent update target")
            # The banner must not claim the release is installable on a
            # mounted/stale runtime that the transaction will reject anyway.
            live = DockerEngine().inspect(target)
            if not (live.get("State") or {}).get("Running"):
                raise DockerEngineError("Agent update target is not running")
            _require_unmanaged_agent_target(live)
            _require_unmounted_baked_agent(live)
            old_image_id = live.get("Image")
            if not isinstance(old_image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", old_image_id):
                raise DockerEngineError("Cannot verify current Agent image identity")
            old_image = DockerEngine().inspect_image(old_image_id)
            if old_image.get("Id") != old_image_id:
                raise DockerEngineError("Cannot verify current Agent image identity")
            old_contract = _baked_agent_contract(old_image)
            if old_contract is None or old_contract[1] == expected["agent_commit"]:
                raise DockerEngineError("No verifiable newer Agent image for this target")
            if payload.get("action") == "agent_preflight":
                return {
                    "ok": True, "target": "agent", "latest_sha": expected["agent_commit"],
                    "latest_version": expected["agent_version"],
                }, None
            old_id = live.get("Id")
            if not isinstance(old_id, str) or not re.fullmatch(r"[0-9a-f]{64}", old_id):
                raise DockerEngineError("Cannot verify Agent update target owner")
            operation_id, initial_progress = progress_store.start(expected["webui_version"])
        except Exception:
            if payload.get("action") == "update_agent":
                busy.release()
            return {"ok": False, "message": "Signed Agent release cannot be authenticated"}, None

        def run_agent_update() -> None:
            try:
                result = replace_container(
                    target, expected["image"], expected_agent_image=expected,
                    expected_old_id=old_id,
                    progress=lambda stage, step, **extra: progress_store.advance(
                        operation_id, stage, step, **extra),
                )
                progress_store.complete(operation_id, cleanup_pending=bool(result.get("cleanup_pending")))
            except Exception:
                logger.exception("Signed Agent update operation %s failed", operation_id)
                progress_store.fail(operation_id)
            finally:
                busy.release()

        return {
            "ok": True, "target": "agent", "restart_scheduled": True,
            "latest_version": expected["agent_version"],
            "latest_sha": expected["agent_commit"],
            "operation_id": operation_id, "progress": initial_progress,
        }, threading.Thread(target=run_agent_update, name="hermes-webui-agent-update", daemon=True)
    if payload.get("action") != "update" or not _release_is_valid(channel, version) or not sha or len(sha) > 128:
        return {"ok": False, "message": "Invalid update request"}, None
    if not busy.acquire(blocking=False):
        return {"ok": False, "message": "Docker update already in progress"}, None
    target = os.getenv("HERMES_WEBUI_UPDATE_TARGET", "hermes-webui").strip()
    repository = os.getenv("HERMES_WEBUI_DOCKER_IMAGE", "24802117/hermes-webui").strip()
    image_tag = version if channel == "stable" else "experimental"
    image = f"{repository}:{image_tag}"
    operation_id, initial_progress = progress_store.start(version)

    def run() -> None:
        try:
            result = replace_container(
                target,
                image,
                version,
                progress=lambda stage, step, **extra: progress_store.advance(
                    operation_id, stage, step, **extra
                ),
            )
            progress_store.complete(
                operation_id,
                cleanup_pending=bool(result.get("cleanup_pending")),
            )
        except Exception:
            logger.exception("Docker self-update operation %s failed", operation_id)
            progress_store.fail(operation_id)
        finally:
            busy.release()

    worker = threading.Thread(target=run, name="hermes-webui-docker-update", daemon=True)
    return {
        "ok": True,
        "latest_version": version,
        "latest_sha": sha,
        "restart_scheduled": True,
        "operation_id": operation_id,
        "progress": initial_progress,
    }, worker


def serve_control(socket_path: str = CONTROL_SOCKET) -> None:
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    socket_gid = int(os.getenv("HERMES_WEBUI_UPDATE_SOCKET_GID", "1024"))
    token_path = Path(os.getenv("HERMES_WEBUI_UPDATE_TOKEN_FILE", CONTROL_TOKEN))
    token = secrets.token_hex(32)
    token_path.write_text(token, encoding="utf-8")
    os.chown(token_path, 0, socket_gid)
    os.chmod(token_path, 0o640)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chown(path, 0, socket_gid)
    os.chmod(path, 0o660)
    server.listen(4)
    busy = threading.Lock()
    progress = UpdateProgress()
    while True:
        conn, _ = server.accept()
        conn.settimeout(5)
        worker = None
        try:
            raw = conn.recv(8192)
            payload = json.loads(raw.splitlines()[0].decode("utf-8"))
            response, worker = _control_request(
                payload,
                busy=busy,
                expected_token=token,
                progress=progress,
            )
        except Exception:
            response = {"ok": False, "message": "Invalid updater request"}
        conn.sendall(json.dumps(response).encode("utf-8"))
        conn.close()
        if worker is not None:
            worker.start()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", metavar="SOCKET")
    parser.add_argument("--healthcheck", metavar="SOCKET")
    args = parser.parse_args(argv)
    if args.serve:
        serve_control(args.serve)
    if args.healthcheck:
        try:
            result = request_health(args.healthcheck)
        except Exception:
            return 1
        return 0 if result.get("ok") is True and result.get("status") == "ready" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
