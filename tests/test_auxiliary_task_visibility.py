"""Detached subagent/process inventory must be scoped to WebUI ownership."""
from types import SimpleNamespace


def test_auxiliary_tasks_profile_scoped_and_batch_count(monkeypatch):
    import sys
    from api import routes
    from tools import async_delegation as delegation
    from tools import process_registry as processes
    rows = {
        "aaaaaaaaaaaa": SimpleNamespace(profile="default"),
        "bbbbbbbbbbbb": SimpleNamespace(profile="other"),
    }
    monkeypatch.setattr(routes, "get_session", lambda sid, **kw: rows[sid])
    from contextlib import nullcontext
    monkeypatch.setattr(delegation, "_transaction", lambda: nullcontext(SimpleNamespace(
        execute=lambda *_args: SimpleNamespace(fetchone=lambda: None))))
    monkeypatch.setattr(delegation, "_records", {
        "batch": {"status": "running", "delegation_id": "batch", "origin_session_id": "api-request-id",
                  "origin_ui_session_id": "aaaaaaaaaaaa", "is_batch": True, "goals": ["one", "two"], "task_indexes": [0, 1]},
        "foreign": {"status": "running", "delegation_id": "foreign", "origin_ui_session_id": "bbbbbbbbbbbb"},
        "done": {"status": "completed", "delegation_id": "done", "origin_ui_session_id": "aaaaaaaaaaaa"},
        "api-only": {"status": "running", "delegation_id": "api-only", "origin_session_id": "api-request-id"},
        "gateway-only": {"status": "running", "delegation_id": "gateway-only", "session_key": "web-a"},
    })
    fake=SimpleNamespace(list_sessions=lambda: [{"session_id":"proc-a","status":"running"},
                                                {"session_id":"proc-b","status":"running"},
                                                {"session_id":"cli","status":"running"}],
                         get=lambda sid: SimpleNamespace(session_key="aaaaaaaaaaaa" if sid=="proc-a" else
                                                         "bbbbbbbbbbbb" if sid=="proc-b" else "cli:task"))
    monkeypatch.setattr(processes, "process_registry", fake)
    inventory=routes._active_auxiliary_task_inventory("default")
    assert inventory == [
        {"type":"delegation","id":"batch","parent_session_id":"aaaaaaaaaaaa","count":2},
        {"type":"process","id":"proc-a","parent_session_id":"aaaaaaaaaaaa","count":1},
    ]


def test_delegation_batch_counts_only_unfinished_children(monkeypatch):
    from contextlib import nullcontext
    from api import routes
    from tools import async_delegation as delegation
    monkeypatch.setattr(routes, "get_session", lambda sid, **kw: SimpleNamespace(profile="default"))
    monkeypatch.setattr("tools.process_registry.process_registry", SimpleNamespace(list_sessions=lambda: []))
    monkeypatch.setattr(delegation, "_records", {
        "batch": {"status":"running", "delegation_id":"batch", "origin_ui_session_id":"web-a",
                  "is_batch":True, "goals":["one","two"], "task_indexes":[0,1]},
    })
    monkeypatch.setattr(delegation, "_transaction", lambda: nullcontext(SimpleNamespace(
        execute=lambda *_args: SimpleNamespace(fetchone=lambda: ('{"results":[{"task_index":0}],"partial":true}',)))))
    assert routes._active_auxiliary_task_inventory("default")[0]["count"] == 1


def test_missing_running_process_record_is_not_a_false_zero(monkeypatch):
    from api import routes
    from tools import async_delegation as delegation
    from tools import process_registry as processes
    import pytest
    monkeypatch.setattr(delegation, "_records", {})
    monkeypatch.setattr(processes, "process_registry", SimpleNamespace(
        list_sessions=lambda: [{"session_id": "proc", "status": "running"}], get=lambda _id: None))
    with pytest.raises(RuntimeError, match="registry entry unavailable"):
        routes._active_auxiliary_task_inventory("default")
