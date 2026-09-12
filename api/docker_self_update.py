"""Restricted updater sidecar using the mounted Docker Engine socket.

The WebUI process never receives Docker daemon access. A same-image sidecar
accepts one authenticated local action for its fixed target and repository,
then performs pull, health-gated replacement, and rollback if needed.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

SOCKET_PATH = "/var/run/docker.sock"
API_PREFIX = "/v1.41"
CONTROL_SOCKET = "/run/hermes-webui-updater/control.sock"
CONTROL_TOKEN = "/run/hermes-webui-updater/token"


class DockerEngineError(RuntimeError):
    pass


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


def _create_payload(info: dict[str, Any], image: str) -> dict[str, Any]:
    config = copy.deepcopy(info.get("Config") or {})
    host = copy.deepcopy(info.get("HostConfig") or {})
    config["Image"] = image
    config.pop("Hostname", None)
    allowed_host = {
        "Binds", "Mounts", "PortBindings", "RestartPolicy", "LogConfig",
        "NetworkMode", "ExtraHosts", "Dns", "DnsSearch", "DnsOptions",
        "CapAdd", "CapDrop", "SecurityOpt", "ReadonlyRootfs", "Init",
        "PidsLimit", "ShmSize", "Memory", "NanoCpus", "CpuShares",
        "Devices", "DeviceRequests", "GroupAdd", "Privileged", "UsernsMode", "Tmpfs",
        "IpcMode", "PidMode", "UTSMode", "CgroupnsMode", "Runtime",
        "MaskedPaths", "ReadonlyPaths", "Ulimits", "Sysctls",
        "CpuPeriod", "CpuQuota", "CpuRealtimePeriod", "CpuRealtimeRuntime",
        "CpusetCpus", "CpusetMems", "MemoryReservation", "MemorySwap",
        "MemorySwappiness", "OomKillDisable", "OomScoreAdj", "BlkioWeight",
    }
    host = {key: value for key, value in host.items() if key in allowed_host and value not in (None, {}, [])}
    payload: dict[str, Any] = config
    payload["HostConfig"] = host
    networks = (info.get("NetworkSettings") or {}).get("Networks") or {}
    if networks:
        payload["NetworkingConfig"] = {"EndpointsConfig": {name: {"Aliases": value.get("Aliases", [])} for name, value in networks.items()}}
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


def _verify_runtime_contract(old: dict[str, Any], new: dict[str, Any]) -> None:
    keys = (
        "Binds", "Mounts", "PortBindings", "RestartPolicy", "NetworkMode",
        "IpcMode", "PidMode", "UTSMode", "CgroupnsMode", "Runtime",
        "ReadonlyRootfs", "Privileged", "ShmSize", "PidsLimit", "Memory",
        "NanoCpus", "CpuShares", "CpusetCpus", "CpusetMems", "GroupAdd",
        "SecurityOpt", "Tmpfs", "Ulimits", "Sysctls", "DeviceRequests",
    )
    old_host = old.get("HostConfig") or {}
    new_host = new.get("HostConfig") or {}
    mismatches = [
        key
        for key in keys
        if _normalize_runtime_value(old_host.get(key))
        != _normalize_runtime_value(new_host.get(key))
    ]
    if _normalized_networks(old) != _normalized_networks(new):
        mismatches.append("Networks")
    if mismatches:
        raise DockerEngineError(
            "replacement runtime contract mismatch: " + ", ".join(mismatches)
        )


def _start_if_stopped(engine: DockerEngine, name: str) -> None:
    state = engine.inspect(name).get("State") or {}
    if not state.get("Running"):
        engine.start(name)


def _verified_image_id(engine: DockerEngine, image: str, version: str) -> str:
    inspected = engine.inspect_image(image)
    labels = (inspected.get("Config") or {}).get("Labels") or {}
    actual = str(labels.get("org.opencontainers.image.version") or "").strip()
    expected = version.removeprefix("exp-").removeprefix("v")
    if actual not in {version, expected}:
        raise DockerEngineError("pulled image version does not match the verified GitHub release")
    image_id = str(inspected.get("Id") or "").strip()
    if not image_id.startswith("sha256:"):
        raise DockerEngineError("pulled image has no immutable image ID")
    return image_id


def replace_container(target: str, image: str, version: str | None = None, *, timeout: int = 180) -> dict[str, Any]:
    engine = DockerEngine()
    old = engine.inspect(target)
    was_running = bool((old.get("State") or {}).get("Running"))
    if not was_running:
        raise DockerEngineError("target container is not running")
    name = _container_name(old)
    backup = f"{name}.hermes-update-old"
    temp = f"{name}.hermes-update-new"
    engine.pull(image)
    create_image = image
    if version:
        create_image = _verified_image_id(engine, image, version)
    engine.rename(target, backup)
    try:
        engine.stop(backup)
        engine.create(temp, _create_payload(old, create_image))
        engine.rename(temp, name)
        engine.start(name)
        deadline = time.monotonic() + timeout
        became_healthy = False
        while time.monotonic() < deadline:
            if _healthy(engine, name):
                became_healthy = True
                break
            time.sleep(2)
        if not became_healthy:
            raise DockerEngineError("replacement container did not become healthy")
        _verify_runtime_contract(old, engine.inspect(name))
    except Exception:
        try:
            engine.remove(name, force=True)
        except Exception:
            pass
        try:
            engine.remove(temp, force=True)
        except Exception:
            pass
        try:
            engine.rename(backup, name)
            if was_running:
                _start_if_stopped(engine, name)
        except Exception as rollback_error:
            raise DockerEngineError(f"update failed and rollback failed: {rollback_error}")
        raise
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


def _release_is_valid(channel: str, version: str) -> bool:
    if channel == "stable":
        return bool(__import__("re").fullmatch(r"v[0-9][0-9A-Za-z.+-]*", version))
    return channel == "experimental" and bool(__import__("re").fullmatch(r"exp-v[0-9][0-9A-Za-z.+-]*", version))


def _control_request(payload: dict[str, Any], *, busy: threading.Lock, expected_token: str) -> tuple[dict[str, Any], threading.Thread | None]:
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
    if payload.get("action") != "update" or not _release_is_valid(channel, version) or not sha or len(sha) > 128:
        return {"ok": False, "message": "Invalid update request"}, None
    if not busy.acquire(blocking=False):
        return {"ok": False, "message": "Docker update already in progress"}, None
    target = os.getenv("HERMES_WEBUI_UPDATE_TARGET", "hermes-webui").strip()
    repository = os.getenv("HERMES_WEBUI_DOCKER_IMAGE", "24802117/hermes-webui").strip()
    image = f"{repository}:{'latest' if channel == 'stable' else 'experimental'}"

    def run() -> None:
        try:
            replace_container(target, image, version)
        finally:
            busy.release()

    worker = threading.Thread(target=run, name="hermes-webui-docker-update", daemon=True)
    return {"ok": True, "latest_version": version, "latest_sha": sha, "restart_scheduled": True}, worker


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
    while True:
        conn, _ = server.accept()
        conn.settimeout(5)
        worker = None
        try:
            raw = conn.recv(8192)
            payload = json.loads(raw.splitlines()[0].decode("utf-8"))
            response, worker = _control_request(payload, busy=busy, expected_token=token)
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
