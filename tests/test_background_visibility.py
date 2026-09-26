"""Read-only inventory of /background tasks, including consumed results."""
from types import SimpleNamespace


def test_background_status_survives_result_delivery_and_scopes_parent(monkeypatch):
    from api import background as bg
    from api import config as cfg
    from api import routes
    monkeypatch.setattr(bg, "_BACKGROUND_TASKS", {})
    monkeypatch.setattr(bg, "_BACKGROUND_RECENT", {})
    monkeypatch.setattr(routes, "get_session", lambda sid, **kw: {
        "parent": SimpleNamespace(profile="default", session_id="parent"),
        "other": SimpleNamespace(profile="private", session_id="other"),
    }[sid])
    monkeypatch.setattr(routes, "_active_auxiliary_task_inventory", lambda _profile: [])
    bg.track_background("parent", "child", "stream-a", "task-a", "task prompt")
    bg.track_background("other", "child2", "stream-b", "task-b", "private prompt")
    with cfg.ACTIVE_RUNS_LOCK:
        previous = dict(cfg.ACTIVE_RUNS)
        cfg.ACTIVE_RUNS.clear()
        cfg.ACTIVE_RUNS["stream-a"] = {"session_id": "child", "phase": "running"}
    try:
        live = routes._active_webui_session_inventory("default")
        assert live["count"] == 1
        assert live["background_tasks"][0]["status"] == "running"
        assert len(live["background_tasks"]) == 1
        bg.complete_background("parent", "task-a", "answer")
        assert bg.get_results("parent")[0]["answer"] == "answer"
        done = routes._active_webui_session_inventory("default")
        assert done["count"] == 1  # worker remains registered after answer capture
        assert done["background_tasks"][0]["status"] == "running"
        assert "answer" not in str(done)
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS.pop("stream-a")
        stopped = routes._active_webui_session_inventory("default")
        assert stopped["count"] == 0
        assert stopped["background_tasks"][0]["status"] == "done"
    finally:
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS.clear()
            cfg.ACTIVE_RUNS.update(previous)


def test_background_orphan_not_reported_as_live(monkeypatch):
    from api import background as bg
    monkeypatch.setattr(bg, "_BACKGROUND_TASKS", {})
    monkeypatch.setattr(bg, "_BACKGROUND_RECENT", {})
    bg.track_background("parent", "child", "missing-stream", "task-a", "task")
    assert bg.list_background_status({"parent"}, set())[0]["status"] == "unknown"
    assert bg.list_background_status({"parent"}, {"missing-stream"})[0]["status"] == "running"


def test_unknown_background_task_never_reports_definitive_zero(monkeypatch):
    from api import background as bg, config as cfg, routes
    monkeypatch.setattr(bg, "_BACKGROUND_TASKS", {})
    monkeypatch.setattr(bg, "_BACKGROUND_RECENT", {})
    monkeypatch.setattr(routes, "get_session", lambda _sid, **_kw: SimpleNamespace(profile="default"))
    monkeypatch.setattr(routes, "_active_auxiliary_task_inventory", lambda _profile: [])
    bg.track_background("parent", "child", "missing-stream", "task", "task")
    with cfg.ACTIVE_RUNS_LOCK:
        previous = dict(cfg.ACTIVE_RUNS)
        cfg.ACTIVE_RUNS.clear()
    try:
        result = routes._active_webui_session_inventory("default")
        assert result["count"] is None
        assert result["background_tasks"][0]["status"] == "unknown"
    finally:
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS.clear()
            cfg.ACTIVE_RUNS.update(previous)
