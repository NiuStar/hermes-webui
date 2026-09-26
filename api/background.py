"""Background and ephemeral task tracking for /background and /btw commands."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()

# parent_session_id -> list of task dicts
_BACKGROUND_TASKS: dict[str, list[dict[str, Any]]] = {}
# Read-only inventory of recently completed tasks, even after result delivery.
_BACKGROUND_RECENT: dict[str, list[dict[str, Any]]] = {}
_BACKGROUND_RECENT_TTL = 3600.0

# btw ephemeral session tracking: parent_sid -> {ephemeral_sid, stream_id, question}
_BTW_TRACKING: dict[str, dict[str, Any]] = {}


def track_background(parent_sid: str, bg_sid: str, stream_id: str,
                     task_id: str, prompt: str) -> None:
    with _lock:
        _BACKGROUND_TASKS.setdefault(parent_sid, []).append({
            "task_id": task_id,
            "bg_session_id": bg_sid,
            "stream_id": stream_id,
            "prompt": prompt,
            "status": "running",
            "started_at": time.time(),
            "answer": None,
            "completed_at": None,
        })


def track_btw(parent_sid: str, ephemeral_sid: str, stream_id: str,
              question: str) -> None:
    with _lock:
        _BTW_TRACKING[parent_sid] = {
            "ephemeral_session_id": ephemeral_sid,
            "stream_id": stream_id,
            "question": question,
        }


def complete_background(parent_sid: str, task_id: str, answer: str) -> None:
    with _lock:
        for t in _BACKGROUND_TASKS.get(parent_sid, []):
            if t["task_id"] == task_id and t["status"] == "running":
                t["status"] = "done"
                t["answer"] = answer
                t["completed_at"] = time.time()
                _BACKGROUND_RECENT.setdefault(parent_sid, []).append({
                    "task_id": task_id, "prompt": t["prompt"],
                    "bg_session_id": t["bg_session_id"], "stream_id": t["stream_id"],
                    "answer": answer,
                    "status": "done", "completed_at": t["completed_at"],
                })
                break


def get_results(parent_sid: str) -> list[dict[str, Any]]:
    """Return completed background task results and remove only the done ones
    from tracking.  Tasks still in ``status="running"`` MUST stay in the list
    so that ``complete_background()`` can still find them when the worker
    thread finishes — otherwise the first poll during a long-running task
    silently drops it and the result is lost forever.
    """
    with _lock:
        tasks = _BACKGROUND_TASKS.get(parent_sid, [])
        done = [t for t in tasks if t["status"] == "done"]
        still_running = [t for t in tasks if t["status"] != "done"]
        if still_running:
            _BACKGROUND_TASKS[parent_sid] = still_running
        else:
            _BACKGROUND_TASKS.pop(parent_sid, None)
        return [{
            "task_id": t["task_id"],
            "prompt": t["prompt"],
            "answer": t["answer"],
            "completed_at": t["completed_at"],
        } for t in done]


def get_task_result(parent_sid: str, task_id: str) -> dict[str, Any] | None:
    """Read only the matching completed task; concurrent pollers cannot steal it."""
    with _lock:
        for row in _BACKGROUND_TASKS.get(parent_sid, []):
            if row["task_id"] == task_id and row["status"] == "done":
                return {"task_id": row["task_id"], "prompt": row["prompt"],
                        "answer": row["answer"], "completed_at": row["completed_at"]}
        for row in _BACKGROUND_RECENT.get(parent_sid, []):
            if row["task_id"] == task_id and time.time() - row["completed_at"] < _BACKGROUND_RECENT_TTL:
                return {"task_id": row["task_id"], "prompt": row["prompt"],
                        "answer": row["answer"], "completed_at": row["completed_at"]}
    return None


def get_background_tasks(parent_sid: str) -> list[dict[str, Any]]:
    """Return all background tasks (running and done) for a parent session."""
    with _lock:
        return list(_BACKGROUND_TASKS.get(parent_sid, []))


def _visible_background_row(row: dict, now: float, live_stream_ids: set[str]) -> bool:
    """A live worker outlives the result-retention window."""
    return (row.get("status") == "running" or
            row.get("stream_id") in live_stream_ids or
            now - float(row.get("completed_at") or 0) < _BACKGROUND_RECENT_TTL)


def background_child_ids(live_stream_ids: set[str] | None = None) -> set[str]:
    """Identify tracked children while their worker lives or result is recent."""
    with _lock:
        now = time.time()
        live = live_stream_ids or set()
        return {row["bg_session_id"] for rows in [*_BACKGROUND_TASKS.values(), *_BACKGROUND_RECENT.values()]
                for row in rows if row.get("bg_session_id") and _visible_background_row(row, now, live)}


def background_parent_ids(live_stream_ids: set[str] | None = None) -> set[str]:
    """Snapshot parents with running or recently completed /background work."""
    with _lock:
        now = time.time()
        live = live_stream_ids or set()
        return {parent for parent, rows in [*_BACKGROUND_TASKS.items(), *_BACKGROUND_RECENT.items()]
                if any(_visible_background_row(row, now, live) for row in rows)}


def list_background_status(parent_sids: set[str], live_stream_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """Non-consuming inventory: status remains readable after result polling."""
    now = time.time()
    with _lock:
        for parent, rows in list(_BACKGROUND_RECENT.items()):
            fresh = [row for row in rows if _visible_background_row(row, now, live_stream_ids or set())]
            if fresh:
                live = [row for row in fresh if row.get("stream_id") in (live_stream_ids or set())]
                stopped = [row for row in fresh if row.get("stream_id") not in (live_stream_ids or set())]
                # Never evict an unfinished worker's child identity to make room
                # for completed history; only the latter has bounded retention.
                _BACKGROUND_RECENT[parent] = [*live, *stopped[-100:]]
            else:
                _BACKGROUND_RECENT.pop(parent, None)
        result = []
        for parent in sorted(parent_sids):
            active = _BACKGROUND_TASKS.get(parent, [])
            recent = _BACKGROUND_RECENT.get(parent, [])
            for row in active:
                if row["status"] != "running" and _visible_background_row(row, now, live_stream_ids or set()) and not any(
                    old["task_id"] == row["task_id"] for old in recent
                ):
                    recent = [*recent, row]
            for row in [*active, *recent]:
                if row.get("status") == "done" and not _visible_background_row(row, now, live_stream_ids or set()):
                    continue
                if any(item["task_id"] == row["task_id"] and item["parent_session_id"] == parent
                       for item in result):
                    continue
                status = row["status"]
                if live_stream_ids is not None:
                    live = row.get("stream_id") in live_stream_ids
                    if status == "running" and not live:
                        status = "unknown"
                    elif status == "done" and live:
                        # Result was recorded before the worker unregistered.
                        status = "running"
                result.append({
                    "task_id": row["task_id"], "parent_session_id": parent,
                    "bg_session_id": row.get("bg_session_id"),
                    "prompt": row["prompt"], "status": status,
                    "started_at": row.get("started_at"),
                    "completed_at": row.get("completed_at"),
                })
        return result


def cleanup_btw(parent_sid: str) -> dict[str, Any] | None:
    """Remove and return btw tracking for a parent session."""
    with _lock:
        return _BTW_TRACKING.pop(parent_sid, None)
