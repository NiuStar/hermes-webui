"""Background inventory profile, completion and deduplication boundaries."""
from types import SimpleNamespace
from urllib.parse import urlsplit


def test_completed_child_stays_identified_after_results_consumed(monkeypatch):
    from api import background as bg
    monkeypatch.setattr(bg, "_BACKGROUND_TASKS", {})
    monkeypatch.setattr(bg, "_BACKGROUND_RECENT", {})
    bg.track_background("parent", "child", "stream", "task", "prompt")
    bg.complete_background("parent", "task", "answer")
    assert bg.get_results("parent")[0]["answer"] == "answer"
    assert "child" in bg.background_child_ids()
    assert bg.get_task_result("parent", "task")["answer"] == "answer"


def test_recently_completed_records_expire_after_retention(monkeypatch):
    from api import background as bg
    monkeypatch.setattr(bg, "_BACKGROUND_TASKS", {})
    monkeypatch.setattr(bg, "_BACKGROUND_RECENT", {})
    bg.track_background("parent", "child", "stream", "task", "prompt")
    bg.complete_background("parent", "task", "answer")
    assert bg.get_results("parent")
    completed = bg._BACKGROUND_RECENT["parent"][0]["completed_at"]
    monkeypatch.setattr(bg.time, "time", lambda: completed + bg._BACKGROUND_RECENT_TTL + 1)
    assert bg.background_parent_ids() == set()
    assert bg.background_child_ids() == set()
    assert bg.get_task_result("parent", "task") is None
    assert bg.list_background_status({"parent"}, set()) == []


def test_result_older_than_retention_keeps_live_child_single_count(monkeypatch):
    from api import background as bg
    monkeypatch.setattr(bg, "_BACKGROUND_TASKS", {})
    monkeypatch.setattr(bg, "_BACKGROUND_RECENT", {})
    bg.track_background("parent", "child", "stream", "task", "prompt")
    bg.complete_background("parent", "task", "answer")
    assert bg.get_results("parent")
    completed = bg._BACKGROUND_RECENT["parent"][0]["completed_at"]
    monkeypatch.setattr(bg.time, "time", lambda: completed + bg._BACKGROUND_RECENT_TTL + 1)
    assert bg.background_child_ids({"stream"}) == {"child"}
    assert bg.background_parent_ids({"stream"}) == {"parent"}
    live = bg.list_background_status({"parent"}, {"stream"})
    assert len(live) == 1 and live[0]["status"] == "running"
    assert bg.background_child_ids(set()) == set()


def test_recent_capacity_never_evicts_live_child_identity(monkeypatch):
    from api import background as bg
    monkeypatch.setattr(bg, "_BACKGROUND_TASKS", {})
    monkeypatch.setattr(bg, "_BACKGROUND_RECENT", {})
    for index in range(102):
        bg.track_background("parent", f"child-{index}", f"stream-{index}", f"task-{index}", "prompt")
        bg.complete_background("parent", f"task-{index}", "answer")
    assert len(bg.get_results("parent")) == 102
    live_streams = {f"stream-{index}" for index in range(102)}
    assert len(bg.list_background_status({"parent"}, live_streams)) == 102
    assert len(bg.background_child_ids(live_streams)) == 102


def test_same_task_id_in_different_parents_remains_visible(monkeypatch):
    from api import background as bg
    monkeypatch.setattr(bg, "_BACKGROUND_TASKS", {})
    monkeypatch.setattr(bg, "_BACKGROUND_RECENT", {})
    for parent in ("a", "b"):
        bg.track_background(parent, "child-" + parent, "stream-" + parent, "same-id", "prompt")
        bg.complete_background(parent, "same-id", "answer")
    rows = bg.list_background_status({"a", "b"}, set())
    assert len(rows) == 2
    assert {row["parent_session_id"] for row in rows} == {"a", "b"}


def test_background_result_status_rejects_other_profile_before_read(monkeypatch):
    from api import routes
    from api import profiles
    responses = []
    monkeypatch.setattr(routes, "get_session", lambda sid, **kw: SimpleNamespace(profile="other"))
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "bad", lambda _handler, message, status=400: responses.append((status, message)))
    monkeypatch.setattr(routes, "j", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("result leaked")))
    from api import background
    monkeypatch.setattr(background, "get_results", lambda *_a: (_ for _ in ()).throw(AssertionError("consumed")))
    monkeypatch.setattr(background, "get_task_result", lambda *_a: (_ for _ in ()).throw(AssertionError("read")))
    for query in ("session_id=parent&task_id=task", "session_id=parent"):
        routes.handle_get(object(), urlsplit("/api/background/status?" + query))
    assert responses == [(404, "Session not found"), (404, "Session not found")]
