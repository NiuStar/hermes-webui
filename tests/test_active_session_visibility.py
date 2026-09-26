"""Visible background-run inventory must use worker liveness, not SSE presence."""
from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_active_session_inventory_scoped_and_bounded(monkeypatch):
    from api import config as cfg
    from api import routes

    rows = {
        "live": SimpleNamespace(session_id="live", title="Visible run", profile="default", archived=False),
        "foreign": SimpleNamespace(session_id="foreign", title="Other profile", profile="other", archived=False),
        "archived": SimpleNamespace(session_id="archived", title="Archived run", profile="default", archived=True),
    }
    monkeypatch.setattr(routes, "get_session", lambda sid, **kw: rows[sid])
    monkeypatch.setattr(routes, "_redact_text", lambda text, **kw: text)
    monkeypatch.setattr(routes, "_active_auxiliary_task_inventory", lambda _profile: [])
    monkeypatch.setattr("api.background.background_parent_ids", lambda _live=None: set())
    monkeypatch.setattr("api.background.background_child_ids", lambda _live=None: set())
    with cfg.ACTIVE_RUNS_LOCK:
        previous = dict(cfg.ACTIVE_RUNS)
        cfg.ACTIVE_RUNS.clear()
        cfg.ACTIVE_RUNS.update({
            "stream-live": {"session_id": "live", "started_at": 100.0, "phase": "running", "workspace": "/secret"},
            "stream-live-2": {"session_id": "live", "started_at": 110.0, "phase": "running"},
            "stream-foreign": {"session_id": "foreign", "started_at": 120.0, "phase": "running"},
            "stream-archived": {"session_id": "archived", "started_at": 130.0, "phase": "running"},
            "stream-cancel": {"session_id": "live", "started_at": 140.0, "phase": "cancelling"},
        })
    try:
        result = routes._active_webui_session_inventory("default")
    finally:
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS.clear()
            cfg.ACTIVE_RUNS.update(previous)
    assert result == {"count": 2, "active_profile": "default", "background_tasks": [], "auxiliary_tasks": [], "sessions": [
        {"session_id": "archived", "title": "Archived run", "started_at": 130.0, "archived": True},
        {"session_id": "live", "title": "Visible run", "started_at": 100.0, "archived": False},
    ]}
    assert "secret" not in str(result)


def test_active_session_inventory_never_resurrects_stale_sidecar(monkeypatch):
    from api import config as cfg
    from api import routes
    monkeypatch.setattr(routes, "get_session", lambda sid, **kw: (_ for _ in ()).throw(KeyError(sid)))
    monkeypatch.setattr(routes, "_active_auxiliary_task_inventory", lambda _profile: [])
    monkeypatch.setattr("api.background.background_parent_ids", lambda _live=None: set())
    monkeypatch.setattr("api.background.background_child_ids", lambda _live=None: set())
    with cfg.ACTIVE_RUNS_LOCK:
        previous = dict(cfg.ACTIVE_RUNS)
        cfg.ACTIVE_RUNS.clear()
    try:
        assert routes._active_webui_session_inventory("default")["count"] == 0
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS["orphan-stream"] = {"session_id": "missing", "phase": "running"}
        with pytest.raises(RuntimeError, match="metadata unavailable"):
            routes._active_webui_session_inventory("default")
    finally:
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS.clear()
            cfg.ACTIVE_RUNS.update(previous)
