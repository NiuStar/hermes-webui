"""Bounded, aggregate-only maintenance for large WebUI session sidecars."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from api import models

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD_BYTES = 10 * 1024 * 1024
DEFAULT_RETRY_SECONDS = 300.0
DEFAULT_START_DELAY_SECONDS = 15.0

_WORKER_LOCK = threading.Lock()
_WORKER_THREAD: threading.Thread | None = None
_STOP_EVENT = threading.Event()
_LAST_RESULT_LOCK = threading.Lock()
_LAST_RESULT: dict | None = None


def _safe_nonnegative_float(value, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _metadata_is_busy(path: Path) -> bool:
    try:
        metadata = json.loads(models._read_metadata_json_prefix(path) or "{}")
    except Exception:
        return True
    return bool(
        metadata.get("active_stream_id")
        or metadata.get("pending_user_message")
        or metadata.get("pending_started_at")
    )


def _runtime_session_is_busy(session_id: str) -> bool:
    try:
        from api.config import SESSION_WRITEBACK_OWNERS, SESSION_WRITEBACK_OWNERS_LOCK
        with SESSION_WRITEBACK_OWNERS_LOCK:
            return bool(SESSION_WRITEBACK_OWNERS.get(session_id))
    except Exception:
        # Runtime ownership uncertainty must defer maintenance, never race a writer.
        return True


def run_sidecar_maintenance_once(
    *,
    session_dir: Path | None = None,
    threshold_bytes: int = DEFAULT_THRESHOLD_BYTES,
    dry_run: bool = False,
) -> dict:
    """Build missing/stale derived indexes without modifying sidecar bodies."""
    root = Path(session_dir or models.SESSION_DIR)
    threshold = max(1, int(threshold_bytes))
    started = time.time()
    result = {
        "status": "ok",
        "scanned_sidecars": 0,
        "candidate_sidecars": 0,
        "already_indexed": 0,
        "repairable_indexes": 0,
        "repaired_indexes": 0,
        "skipped_active": 0,
        "skipped_changed": 0,
        "failed": 0,
        "max_candidate_bytes": 0,
        "started_at": started,
        "finished_at": None,
    }
    try:
        paths = sorted(root.glob("*.json"))
    except Exception:
        result["status"] = "error"
        result["failed"] = 1
        result["finished_at"] = time.time()
        return result

    if root != Path(models.SESSION_DIR):
        result["status"] = "error"
        result["failed"] = 1
        result["finished_at"] = time.time()
        return result
    for path in paths:
        if path.name == "_index.json":
            continue
        result["scanned_sidecars"] += 1
        try:
            before = models._sidecar_stat_signature(path)
            if before is None or int(before[2]) < threshold:
                continue
            sid = path.stem
            if not models.is_safe_session_id(sid):
                result["failed"] += 1
                continue
            result["candidate_sidecars"] += 1
            result["max_candidate_bytes"] = max(
                result["max_candidate_bytes"], int(before[2])
            )
            if models.message_offset_index_is_current(sid):
                result["already_indexed"] += 1
                continue
            if _runtime_session_is_busy(sid) or _metadata_is_busy(path):
                result["skipped_active"] += 1
                continue
            if models._sidecar_stat_signature(path) != before:
                result["skipped_changed"] += 1
                continue
            result["repairable_indexes"] += 1
            if dry_run:
                continue
            if models.ensure_message_offset_index(sid):
                result["repaired_indexes"] += 1
            elif models._sidecar_stat_signature(path) != before:
                result["skipped_changed"] += 1
            else:
                result["failed"] += 1
        except Exception:
            result["failed"] += 1
            logger.debug("Sidecar maintenance candidate failed", exc_info=True)
    if result["failed"]:
        result["status"] = "partial"
    result["finished_at"] = time.time()
    with _LAST_RESULT_LOCK:
        global _LAST_RESULT
        _LAST_RESULT = dict(result)
    return result


def sidecar_maintenance_status() -> dict:
    with _LAST_RESULT_LOCK:
        result = dict(_LAST_RESULT) if _LAST_RESULT is not None else None
    with _WORKER_LOCK:
        running = bool(_WORKER_THREAD and _WORKER_THREAD.is_alive())
    return {"worker_running": running, "last_run": result}


def _sidecar_maintenance_loop(
    *,
    delay_seconds: float,
    retry_seconds: float,
    threshold_bytes: int,
) -> None:
    if _STOP_EVENT.wait(delay_seconds):
        return
    while not _STOP_EVENT.is_set():
        result = run_sidecar_maintenance_once(threshold_bytes=threshold_bytes)
        logger.info(
            "sidecar maintenance: scanned=%s candidates=%s repaired=%s active=%s changed=%s failed=%s",
            result.get("scanned_sidecars", 0),
            result.get("candidate_sidecars", 0),
            result.get("repaired_indexes", 0),
            result.get("skipped_active", 0),
            result.get("skipped_changed", 0),
            result.get("failed", 0),
        )
        if _STOP_EVENT.wait(retry_seconds):
            return


def start_sidecar_maintenance_worker(
    *,
    delay_seconds: float | None = None,
    retry_seconds: float | None = None,
    threshold_bytes: int = DEFAULT_THRESHOLD_BYTES,
) -> bool:
    """Start one daemon maintenance worker; return False when disabled/running."""
    if os.environ.get("HERMES_WEBUI_SIDECAR_MAINTENANCE", "1").strip().lower() in {
        "0", "false", "no", "off"
    }:
        return False
    delay = _safe_nonnegative_float(
        os.environ.get("HERMES_WEBUI_SIDECAR_MAINTENANCE_DELAY", delay_seconds),
        DEFAULT_START_DELAY_SECONDS if delay_seconds is None else float(delay_seconds),
    )
    retry = max(
        1.0,
        _safe_nonnegative_float(
            os.environ.get("HERMES_WEBUI_SIDECAR_MAINTENANCE_INTERVAL", retry_seconds),
            DEFAULT_RETRY_SECONDS if retry_seconds is None else float(retry_seconds),
        ),
    )
    with _WORKER_LOCK:
        global _WORKER_THREAD
        if _WORKER_THREAD and _WORKER_THREAD.is_alive():
            return False
        _STOP_EVENT.clear()
        _WORKER_THREAD = threading.Thread(
            target=_sidecar_maintenance_loop,
            kwargs={
                "delay_seconds": delay,
                "retry_seconds": retry,
                "threshold_bytes": max(1, int(threshold_bytes)),
            },
            name="session-sidecar-maintenance",
            daemon=True,
        )
        _WORKER_THREAD.start()
        return True


def stop_sidecar_maintenance_worker() -> None:
    _STOP_EVENT.set()


def _reset_sidecar_maintenance_worker_for_tests() -> None:
    global _WORKER_THREAD, _LAST_RESULT
    _STOP_EVENT.set()
    with _WORKER_LOCK:
        thread = _WORKER_THREAD
    if thread and thread.is_alive():
        thread.join(timeout=1)
    with _WORKER_LOCK:
        _WORKER_THREAD = None
    with _LAST_RESULT_LOCK:
        _LAST_RESULT = None
    _STOP_EVENT.clear()
