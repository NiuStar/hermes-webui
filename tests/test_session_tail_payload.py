import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

from api import models, routes


class _FakeSession:
    def __init__(self, messages):
        self.session_id = "tail_payload_001"
        self.title = "Tail payload"
        self.workspace = "/tmp"
        self.model = "gpt-test"
        self.model_provider = None
        self.messages = messages
        self.tool_calls = [
            {"name": "old-tool", "snippet": "historical snippet", "assistant_msg_idx": 0},
            {"name": "visible-tool", "snippet": "visible snippet", "assistant_msg_idx": 1},
        ]
        self.input_tokens = 0
        self.output_tokens = 0
        self.estimated_cost = 0
        self.context_length = 1
        self.threshold_tokens = 0
        self.last_prompt_tokens = 0
        self.active_stream_id = None
        self.pending_user_message = None
        self.pending_attachments = []
        self.pending_started_at = None
        self.composer_draft = {}

    def compact(self):
        return {
            "session_id": self.session_id,
            "title": self.title,
            "workspace": self.workspace,
            "model": self.model,
            "model_provider": self.model_provider,
            "message_count": len(self.messages),
            "context_length": self.context_length,
            "threshold_tokens": self.threshold_tokens,
            "last_prompt_tokens": self.last_prompt_tokens,
            "active_stream_id": self.active_stream_id,
            "pending_user_message": self.pending_user_message,
            "composer_draft": self.composer_draft,
        }


def _invoke(session, query=None):
    import api.routes as routes

    captured = {}

    def fake_j(_handler, data, status=200, extra_headers=None):
        captured["data"] = data
        captured["status"] = status
        return data

    if query is None:
        query = "session_id=tail_payload_001&messages=1&resolve_model=0&msg_limit=1"
    parsed = urlparse(f"/api/session?{query}")
    with patch("api.routes.get_session", return_value=session), \
         patch("api.routes._clear_stale_stream_state", return_value=False), \
         patch("api.routes._lookup_cli_session_metadata", return_value={}), \
         patch("api.routes.get_state_db_session_messages", return_value=[]), \
         patch("api.routes.redact_session_data", side_effect=lambda raw: raw), \
         patch("api.routes.j", side_effect=fake_j):
        routes.handle_get(SimpleNamespace(), parsed)
    return captured["data"]["session"]


def test_tail_window_includes_windowed_session_tool_calls_even_when_messages_have_tool_metadata():
    session = _FakeSession([
        {"role": "user", "content": "older"},
        {
            "role": "assistant",
            "content": "visible",
            "tool_calls": [{"id": "call_1", "function": {"name": "tool", "arguments": "{}"}}],
        },
    ])

    payload = _invoke(session)

    assert payload["messages"] == [session.messages[-1]]
    # PR #3665: always return session-level tool_calls (windowed to the
    # message window) so the browser can merge them with per-message ones.
    assert payload["tool_calls"] == [
        {"name": "visible-tool", "snippet": "visible snippet", "assistant_msg_idx": 0}
    ]
    assert payload["_messages_truncated"] is True


def test_tail_window_keeps_only_visible_session_tool_calls_for_legacy_messages_without_metadata():
    session = _FakeSession([
        {"role": "user", "content": "older"},
        {"role": "assistant", "content": "visible legacy message"},
    ])

    payload = _invoke(session)

    assert payload["messages"] == [session.messages[-1]]
    assert payload["tool_calls"] == [
        {"name": "visible-tool", "snippet": "visible snippet", "assistant_msg_idx": 0}
    ]
    assert session.tool_calls[-1]["assistant_msg_idx"] == 1


def test_full_load_keeps_all_session_tool_calls_for_legacy_messages_without_metadata():
    session = _FakeSession([
        {"role": "user", "content": "older"},
        {"role": "assistant", "content": "visible legacy message"},
    ])

    payload = _invoke(
        session,
        query="session_id=tail_payload_001&messages=1&resolve_model=0",
    )

    assert payload["messages"] == session.messages
    assert payload["tool_calls"] == session.tool_calls


def test_bare_large_sidecar_request_is_server_bounded(monkeypatch):
    messages = [
        {"role": "user" if idx % 2 == 0 else "assistant", "content": f"message {idx}"}
        for idx in range(700)
    ]
    session = _FakeSession(messages)
    monkeypatch.setattr("api.routes._sidecar_file_exceeds_threshold", lambda *_args: True)

    payload = _invoke(
        session,
        query="session_id=tail_payload_001&messages=1&resolve_model=0",
    )

    assert payload["messages"] == messages[-500:]
    assert payload["message_count"] == 700
    assert payload["_messages_offset"] == 200
    assert payload["_messages_truncated"] is True
    assert payload["_server_bounded_large_sidecar"] is True


def test_bare_small_tip_with_large_lineage_parent_is_server_bounded(monkeypatch):
    messages = [
        {"role": "user" if idx % 2 == 0 else "assistant", "content": f"message {idx}"}
        for idx in range(700)
    ]
    session = _FakeSession(messages)
    session.parent_session_id = "large_parent"
    monkeypatch.setattr("api.routes._sidecar_lineage_exceeds_threshold", lambda *_args: True)

    payload = _invoke(
        session,
        query="session_id=tail_payload_001&messages=1&resolve_model=0",
    )

    assert payload["messages"] == messages[-500:]
    assert payload["_server_bounded_large_sidecar"] is True


def test_full_query_cannot_bypass_large_sidecar_response_bound(monkeypatch):
    messages = [
        {"role": "user" if idx % 2 == 0 else "assistant", "content": f"message {idx}"}
        for idx in range(700)
    ]
    session = _FakeSession(messages)
    monkeypatch.setattr("api.routes._sidecar_file_exceeds_threshold", lambda *_args: True)

    payload = _invoke(
        session,
        query="session_id=tail_payload_001&messages=1&resolve_model=0&full=1",
    )

    assert payload["messages"] == messages[-500:]
    assert payload["message_count"] == 700
    assert payload["_messages_offset"] == 200
    assert payload["_server_bounded_large_sidecar"] is True


def test_msg_before_window_keeps_only_that_page_session_tool_calls():
    session = _FakeSession([
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second legacy message"},
        {"role": "assistant", "content": "third legacy message"},
        {"role": "assistant", "content": "fourth legacy message"},
    ])
    session.tool_calls = [
        {"name": "first-page-tool", "snippet": "kept", "assistant_msg_idx": 1},
        {"name": "second-page-tool", "snippet": "also kept", "assistant_msg_idx": 2},
        {"name": "tail-tool", "snippet": "not in page", "assistant_msg_idx": 3},
        {"name": "unindexed-tool", "snippet": "cannot place"},
    ]

    payload = _invoke(
        session,
        query="session_id=tail_payload_001&messages=1&resolve_model=0&msg_before=3&msg_limit=2",
    )

    assert payload["messages"] == session.messages[1:3]
    assert payload["tool_calls"] == [
        {"name": "first-page-tool", "snippet": "kept", "assistant_msg_idx": 0},
        {"name": "second-page-tool", "snippet": "also kept", "assistant_msg_idx": 1},
    ]
    assert session.tool_calls[0]["assistant_msg_idx"] == 1
    assert session.tool_calls[1]["assistant_msg_idx"] == 2
    assert payload["_messages_offset"] == 1


def test_msg_limit_tail_does_not_run_heavy_webui_lineage_merge():
    session = _FakeSession([
        {"role": "user", "content": "older"},
        {"role": "assistant", "content": "visible"},
    ])
    session.parent_session_id = "parent"
    session.session_source = "webui"

    with patch(
        "api.routes._merged_webui_lineage_messages_for_display",
        side_effect=AssertionError("limited loads must not merge parent sidecars"),
    ), patch(
        "api.routes.Session.load",
        return_value=None,
    ):
        payload = _invoke(session)

    assert payload["messages"] == [session.messages[-1]]
    assert payload["message_count"] == 2
    assert payload["_messages_truncated"] is True


def test_msg_limit_tail_keeps_pre_compression_snapshot_parent_reachable():
    parent = _FakeSession([
        {"role": "user", "content": "archived question", "timestamp": 1.0},
        {"role": "assistant", "content": "archived answer", "timestamp": 2.0},
    ])
    parent.session_id = "snapshot_parent"
    parent.pre_compression_snapshot = True

    child = _FakeSession([
        {"role": "user", "content": "continuation question", "timestamp": 3.0},
        {"role": "assistant", "content": "continuation answer", "timestamp": 4.0},
    ])
    child.parent_session_id = "snapshot_parent"
    child.pre_compression_snapshot = False
    child.session_source = "webui"

    with patch("api.routes.Session.load", return_value=parent):
        payload = _invoke(
            child,
            query="session_id=tail_payload_001&messages=1&resolve_model=0&msg_limit=30",
        )

    assert [m["content"] for m in payload["messages"]] == [
        "archived question",
        "archived answer",
        "continuation question",
        "continuation answer",
    ]
    assert payload["_messages_offset"] == 0
    assert payload["_messages_truncated"] is False


def test_msg_limit_tail_truncates_large_hidden_tool_results():
    huge_tool_output = "x" * 20_000
    session = _FakeSession([
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "function": {"name": "vision", "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": huge_tool_output,
        },
        {"role": "assistant", "content": "done"},
    ])

    payload = _invoke(
        session,
        query="session_id=tail_payload_001&messages=1&resolve_model=0&msg_limit=2",
    )

    tool_msg = payload["messages"][1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["_content_truncated"] is True
    assert tool_msg["_content_original_chars"] == len(huge_tool_output)
    assert len(tool_msg["content"]) < len(huge_tool_output)
    assert "Tool output truncated" in tool_msg["content"]


def test_msg_limit_tail_does_not_signal_truncated_for_trailing_hidden_tool_rows():
    session = _FakeSession([
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ] + [
        {"role": "tool", "content": f"hidden tool row {idx}"}
        for idx in range(40)
    ])

    payload = _invoke(
        session,
        query="session_id=tail_payload_001&messages=1&resolve_model=0&msg_limit=30",
    )

    assert [m["role"] for m in payload["messages"]] == ["user", "assistant"]
    assert payload["_messages_offset"] == 0
    assert payload["_messages_truncated"] is False


def test_msg_limit_tail_preserves_list_tool_content_type_when_truncated():
    large_list_content = [{"type": "text", "text": "x" * 20_000}]
    session = _FakeSession([
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "function": {"name": "vision", "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": large_list_content,
        },
        {"role": "assistant", "content": "done"},
    ])

    payload = _invoke(
        session,
        query="session_id=tail_payload_001&messages=1&resolve_model=0&msg_limit=2",
    )

    tool_msg = payload["messages"][1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["_content_truncated"] is True
    assert isinstance(tool_msg["content"], list)
    assert not isinstance(tool_msg["content"], str)
    assert tool_msg["content"][0]["type"] == "text"
    assert "Tool output truncated" in tool_msg["content"][0]["text"]


def test_saved_sidecar_supports_indexed_visible_tail_without_full_load(tmp_path, monkeypatch):
    import api.config as config
    import api.models as models
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    models.SESSIONS.clear()

    messages = []
    for idx in range(80):
        call_id = f"call-{idx}"
        messages.extend([
            {
                "role": "assistant",
                "content": f"visible-{idx}",
                "timestamp": float(idx * 3 + 1),
                "tool_calls": [{"id": call_id, "function": {"name": "tool", "arguments": "{}"}}],
            },
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": "x" * 20_000,
                "timestamp": float(idx * 3 + 2),
            },
            {
                "role": "tool",
                "tool_call_id": f"orphan-{idx}",
                "content": "ignored",
                "timestamp": float(idx * 3 + 3),
            },
        ])
    session = models.Session(
        session_id="indexed_tail",
        messages=messages,
        tool_calls=[
            {"assistant_msg_idx": idx * 3, "name": "tool", "snippet": f"snippet-{idx}"}
            for idx in range(80)
        ],
    )
    session.save(touch_updated_at=False)

    def forbidden_full_load(_cls, _sid):
        raise AssertionError("indexed display tail must not call Session.load")

    monkeypatch.setattr(models.Session, "load", classmethod(forbidden_full_load))
    result = models.read_indexed_session_message_window(
        "indexed_tail",
        visible_limit=30,
        is_renderable=routes._message_counts_as_renderable_for_window,
    )
    expected, expected_offset = routes._message_window_for_display(messages, msg_limit=30)

    assert result is not None
    assert result["messages"] == expected
    assert result["message_count"] == len(messages)
    assert result["messages_offset"] == expected_offset
    assert result["tool_calls"] == [
        {"assistant_msg_idx": idx * 3 - expected_offset, "name": "tool", "snippet": f"snippet-{idx}"}
        for idx in range(50, 80)
    ]
    offset_index = json.loads(
        (session_dir / ".message_offsets" / "indexed_tail.json").read_text(encoding="utf-8")
    )
    assert offset_index["message_count"] == len(messages)
    assert offset_index["sidecar_name"] == "indexed_tail.json"


def test_existing_sidecar_builds_index_without_full_load(tmp_path, monkeypatch):
    import api.config as config
    import api.models as models
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)

    messages = [
        {"role": "user" if idx % 2 == 0 else "assistant", "content": f"row-{idx}", "timestamp": float(idx)}
        for idx in range(120)
    ]
    document = {
        "session_id": "legacy_index",
        "title": "Legacy index",
        "workspace": str(tmp_path),
        "model": "test",
        "created_at": 1.0,
        "updated_at": 120.0,
        "message_count": len(messages),
        "messages": messages,
        "tool_calls": [],
        "anchor_activity_scenes": {},
        "context_messages": messages[-4:],
    }
    sidecar = session_dir / "legacy_index.json"
    sidecar.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    def forbidden_full_load(_cls, _sid):
        raise AssertionError("legacy index build must not call Session.load")

    monkeypatch.setattr(models.Session, "load", classmethod(forbidden_full_load))
    result = models.read_indexed_session_message_window(
        "legacy_index",
        visible_limit=30,
        is_renderable=routes._message_counts_as_renderable_for_window,
    )

    assert result is not None
    assert result["messages"] == messages[-30:]
    assert result["message_count"] == 120
    assert result["messages_offset"] == 90
    assert (session_dir / ".message_offsets" / "legacy_index.json").exists()


def test_oversized_derived_index_is_discarded_before_reading(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    sid = "oversized_index"
    session = models.Session(
        session_id=sid,
        title="index",
        messages=[{"role": "assistant", "content": "safe", "timestamp": 1.0}],
    )
    session.save(skip_index=True)
    index_path = models._message_offset_index_path(sid)
    with index_path.open("wb") as handle:
        handle.truncate(models._MESSAGE_OFFSET_INDEX_MAX_BYTES + 1)

    result = models.read_indexed_session_message_window(
        sid,
        visible_limit=30,
        is_renderable=routes._message_counts_as_renderable_for_window,
    )

    assert result is not None
    assert result["messages"] == session.messages
    assert index_path.stat().st_size < models._MESSAGE_OFFSET_INDEX_MAX_BYTES


def test_derived_index_row_limit_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    session = models.Session(
        session_id="row_limit",
        title="rows",
        messages=[
            {"role": "assistant", "content": "one", "timestamp": 1.0},
            {"role": "assistant", "content": "two", "timestamp": 2.0},
        ],
    )
    session.save(skip_index=True)
    monkeypatch.setattr(models, "_MESSAGE_OFFSET_INDEX_MAX_ROWS", 1)

    assert models.read_indexed_session_message_window(
        "row_limit",
        visible_limit=30,
        is_renderable=routes._message_counts_as_renderable_for_window,
    ) is None


def test_legacy_index_builder_stops_during_scan_at_row_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    sid = "scan_row_limit"
    sidecar = tmp_path / f"{sid}.json"
    sidecar.write_text(
        json.dumps({
            "session_id": sid,
            "title": "rows",
            "created_at": 1,
            "updated_at": 1,
            "messages": [
                {"role": "assistant", "content": f"row-{idx}", "timestamp": idx}
                for idx in range(5)
            ],
            "tool_calls": [],
            "anchor_activity_scenes": {},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(models, "_MESSAGE_OFFSET_INDEX_MAX_ROWS", 2)

    assert models._build_message_offset_index_from_sidecar(sid, sidecar) is False
    assert not models._message_offset_index_path(sid).exists()


def test_legacy_index_builder_stops_before_index_byte_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    sid = "scan_byte_limit"
    sidecar = tmp_path / f"{sid}.json"
    sidecar.write_text(
        json.dumps({
            "session_id": sid,
            "title": "bytes",
            "created_at": 1,
            "updated_at": 1,
            "messages": [
                {"role": "assistant", "content": f"unique-{idx}", "timestamp": idx}
                for idx in range(20)
            ],
            "tool_calls": [],
            "anchor_activity_scenes": {},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(models, "_MESSAGE_OFFSET_INDEX_MAX_BYTES", 512)

    assert models._build_message_offset_index_from_sidecar(sid, sidecar) is False
    assert not models._message_offset_index_path(sid).exists()


def test_save_keeps_full_sidecar_but_skips_unindexable_message_count(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "_MESSAGE_OFFSET_INDEX_MAX_ROWS", 2)
    session = models.Session(
        session_id="save_row_limit",
        title="rows",
        messages=[
            {"role": "assistant", "content": f"row-{idx}", "timestamp": idx}
            for idx in range(5)
        ],
    )

    session.save(skip_index=True)

    payload = json.loads(session.path.read_bytes())
    assert len(payload["messages"]) == 5
    assert not models._message_offset_index_path(session.session_id).exists()


def test_index_builder_never_binds_old_offsets_to_replaced_sidecar(tmp_path, monkeypatch):
    import api.config as config
    import api.models as models
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    sid = "replaced_during_index"
    sidecar = session_dir / f"{sid}.json"
    original = {
        "session_id": sid,
        "messages": [{"role": "assistant", "content": "old", "timestamp": 1.0}],
        "tool_calls": [],
        "anchor_activity_scenes": {},
    }
    replacement = {
        "session_id": sid,
        "messages": [{"role": "assistant", "content": "replacement-is-longer", "timestamp": 2.0}],
        "tool_calls": [],
        "anchor_activity_scenes": {},
    }
    sidecar.write_text(json.dumps(original), encoding="utf-8")
    real_write_index = models._write_message_offset_index

    replaced = False

    def replace_then_publish(session_id, sidecar_path, index_data, **kwargs):
        nonlocal replaced
        if not replaced:
            temp = sidecar_path.with_suffix(".replacement")
            temp.write_text(json.dumps(replacement), encoding="utf-8")
            temp.replace(sidecar_path)
            replaced = True
        return real_write_index(session_id, sidecar_path, index_data, **kwargs)

    monkeypatch.setattr(models, "_write_message_offset_index", replace_then_publish)
    assert models._build_message_offset_index_from_sidecar(sid, sidecar) is False
    result = models.read_indexed_session_message_window(
        sid,
        visible_limit=30,
        is_renderable=routes._message_counts_as_renderable_for_window,
    )
    assert result is not None
    assert result["messages"] == replacement["messages"]
    index_data = json.loads(
        (session_dir / ".message_offsets" / f"{sid}.json").read_text(encoding="utf-8")
    )
    assert index_data["sidecar_signature"] == list(models._sidecar_stat_signature(sidecar)[1:])


def test_indexed_window_recovers_latest_todo_snapshot_from_escaped_content(tmp_path, monkeypatch):
    import api.config as config
    import api.models as models
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    session = models.Session(
        session_id="indexed_todo",
        messages=[
            {"role": "tool", "content": json.dumps({"todos": [{"id": "1", "content": "task", "status": "pending"}], "summary": {"total": 1}}), "timestamp": 10.0},
            {"role": "assistant", "content": "done", "timestamp": 11.0},
        ],
    )
    session.save(touch_updated_at=False)

    result = models.read_indexed_session_message_window(
        "indexed_todo",
        visible_limit=1,
        is_renderable=routes._message_counts_as_renderable_for_window,
    )

    assert result is not None
    assert result["todo_state"]["todos"][0]["id"] == "1"


def test_legacy_index_build_recovers_todo_snapshot(tmp_path, monkeypatch):
    import api.config as config
    import api.models as models
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    sid = "legacy_todo_index"
    document = {
        "session_id": sid,
        "messages": [
            {"role": "tool", "content": json.dumps({"todos": [{"id": "legacy", "content": "task", "status": "pending"}], "summary": {"total": 1}}), "timestamp": 10.0},
            {"role": "assistant", "content": "done", "timestamp": 11.0},
        ],
        "tool_calls": [],
        "anchor_activity_scenes": {},
    }
    (session_dir / f"{sid}.json").write_text(json.dumps(document), encoding="utf-8")

    result = models.read_indexed_session_message_window(
        sid,
        visible_limit=1,
        is_renderable=routes._message_counts_as_renderable_for_window,
    )

    assert result is not None
    assert result["todo_state"]["todos"][0]["id"] == "legacy"


def test_indexed_window_fast_path_requires_idle_current_webui_sidecar(monkeypatch):
    import api.routes as routes

    stub = SimpleNamespace(
        session_id="fast_tail",
        session_source="webui",
        raw_source=None,
        source_tag=None,
        is_cli_session=False,
        read_only=False,
        pre_compression_snapshot=False,
        parent_session_id=None,
        active_stream_id=None,
        pending_user_message=None,
        pending_started_at=None,
        updated_at=200.0,
        profile="default",
    )
    indexed = {
        "messages": [{"role": "assistant", "content": "tail", "timestamp": 190.0}],
        "message_count": 1000,
        "messages_offset": 999,
        "tool_calls": [],
        "anchor_activity_scenes": {},
        "todo_state": None,
        "visible_key_counts": {"matching-digest": 1},
    }
    monkeypatch.setattr(routes, "_sidecar_file_exceeds_threshold", lambda *_args: True)
    monkeypatch.setattr(routes, "read_indexed_session_message_window", lambda *_args, **_kwargs: indexed)
    monkeypatch.setattr(
        routes,
        "state_db_active_messages_are_indexed",
        lambda *_args, **_kwargs: True,
    )

    assert routes._indexed_session_window_if_safe(stub, msg_limit=30) is indexed

    for field, value in (
        ("active_stream_id", "live"),
        ("pending_user_message", "queued"),
        ("parent_session_id", "snapshot"),
        ("read_only", True),
        ("session_source", "weixin"),
    ):
        candidate = SimpleNamespace(**stub.__dict__)
        setattr(candidate, field, value)
        assert routes._indexed_session_window_if_safe(candidate, msg_limit=30) is None

    monkeypatch.setattr(routes, "state_db_active_messages_are_indexed", lambda *_args, **_kwargs: False)
    assert routes._indexed_session_window_if_safe(stub, msg_limit=30) is None


def test_indexed_window_does_not_fallback_only_because_state_db_exceeds_5000_rows(monkeypatch):
    import api.routes as routes

    stub = SimpleNamespace(
        session_id="fast_many_state_rows",
        session_source="webui",
        raw_source=None,
        source_tag=None,
        is_cli_session=False,
        read_only=False,
        pre_compression_snapshot=False,
        parent_session_id=None,
        active_stream_id=None,
        pending_user_message=None,
        pending_started_at=None,
        updated_at=200.0,
        profile="default",
    )
    indexed = {
        "messages": [{"role": "assistant", "content": "tail", "timestamp": 190.0}],
        "message_count": 6001,
        "messages_offset": 6000,
        "tool_calls": [],
        "anchor_activity_scenes": {},
        "todo_state": None,
        "visible_key_counts": {"digest": 6001},
        "last_message_timestamp": 190.0,
    }
    monkeypatch.setattr(routes, "_sidecar_file_exceeds_threshold", lambda *_args: True)
    monkeypatch.setattr(routes, "read_indexed_session_message_window", lambda *_args, **_kwargs: indexed)
    proof_calls = []
    monkeypatch.setattr(
        routes,
        "state_db_active_messages_are_indexed",
        lambda *_args, **kwargs: proof_calls.append(kwargs) or True,
    )

    assert routes._indexed_session_window_if_safe(stub, msg_limit=30) is indexed
    assert proof_calls == [{
        "profile": "default",
        "available_visible_key_counts": {"digest": 6001},
        "sidecar_last_message_at": 190.0,
    }]


def test_state_db_index_proof_streams_more_than_5000_active_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT,
            content TEXT,
            timestamp REAL,
            active INTEGER
        )"""
    )
    rows = [
        (idx + 1, "many_rows", "assistant", f"row-{idx}", float(idx), 1)
        for idx in range(6001)
    ]
    conn.executemany(
        "INSERT INTO messages(id, session_id, role, content, timestamp, active) VALUES(?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)
    projected = [
        {"role": role, "content": content, "timestamp": timestamp}
        for _id, _sid, role, content, timestamp, _active in rows
    ]
    counts = models._message_visible_key_counts(
        projected,
        normalize_workspace_prefix=True,
    )

    assert models.state_db_active_messages_are_indexed(
        "many_rows",
        available_visible_key_counts=counts,
        sidecar_last_message_at=6000.0,
        batch_size=500,
    ) is True

    missing = dict(counts)
    missing.pop(models._message_visible_key_digest(projected[0], normalize_workspace_prefix=True))
    assert models.state_db_active_messages_are_indexed(
        "many_rows",
        available_visible_key_counts=missing,
        sidecar_last_message_at=6000.0,
        batch_size=500,
    ) is False
    assert models.state_db_active_messages_are_indexed(
        "many_rows",
        available_visible_key_counts=counts,
        sidecar_last_message_at=5999.0,
        batch_size=500,
    ) is False


def test_state_db_index_proof_ignores_private_row_identity_but_matches_api_content(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT,
            content TEXT,
            timestamp REAL,
            active INTEGER,
            api_content TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO messages VALUES(1, 'api_row', 'assistant', 'visible', 10.0, 1, 'provider text')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)
    sidecar_message = {
        "role": "assistant",
        "content": "visible",
        "timestamp": 10.0,
        "api_content": "provider text",
    }
    counts = models._message_visible_key_counts(
        [sidecar_message], normalize_workspace_prefix=True
    )

    assert models.state_db_active_messages_are_indexed(
        "api_row",
        available_visible_key_counts=counts,
        sidecar_last_message_at=10.0,
    ) is True


def test_session_route_uses_metadata_only_when_indexed_window_is_safe(monkeypatch):
    import api.routes as routes

    session = _FakeSession([])
    session._metadata_message_count = 1000
    calls = []
    indexed = {
        "messages": [{"role": "assistant", "content": "indexed tail", "timestamp": 100.0}],
        "message_count": 1000,
        "messages_offset": 999,
        "tool_calls": [],
        "anchor_activity_scenes": {},
        "todo_state": None,
    }
    captured = {}

    def get_session(_sid, metadata_only=False):
        calls.append(metadata_only)
        if not metadata_only:
            raise AssertionError("safe indexed route must not full-load the sidecar")
        return session

    def fake_j(_handler, data, status=200, extra_headers=None):
        captured["data"] = data
        return data

    parsed = urlparse(
        "/api/session?session_id=tail_payload_001&messages=1&resolve_model=0&msg_limit=30"
    )
    with patch("api.routes.get_session", side_effect=get_session), \
         patch("api.routes._indexed_session_window_if_safe", return_value=indexed), \
         patch("api.routes._clear_stale_stream_state", return_value=False), \
         patch("api.routes._lookup_cli_session_metadata", return_value={}), \
         patch("api.routes.redact_session_data", side_effect=lambda raw: raw), \
         patch("api.routes.j", side_effect=fake_j):
        routes.handle_get(SimpleNamespace(), parsed)

    payload = captured["data"]["session"]
    assert calls == [True]
    assert payload["messages"] == indexed["messages"]
    assert payload["message_count"] == 1000
    assert payload["_messages_offset"] == 999
    assert payload["_messages_truncated"] is True


def test_indexed_lineage_window_pages_across_segments_without_full_load(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    root_messages = [
        {"role": "user", "content": "r0", "timestamp": 1},
        {"role": "assistant", "content": "r1", "timestamp": 2},
        {"role": "system", "content": "marker-1", "timestamp": 3},
        {"role": "user", "content": "c0", "timestamp": 4},
        {"role": "assistant", "content": "c1", "timestamp": 5},
    ]
    child_messages = [
        *root_messages[-3:],
        {"role": "user", "content": "later0", "timestamp": 6},
        {"role": "assistant", "content": "later1", "timestamp": 7},
        {"role": "system", "content": "marker-2", "timestamp": 8},
        {"role": "user", "content": "t0", "timestamp": 9},
        {"role": "assistant", "content": "t1", "timestamp": 10},
    ]
    tip_messages = [
        *child_messages[-3:],
        {"role": "user", "content": "future0", "timestamp": 11},
        {"role": "assistant", "content": "future1", "timestamp": 12},
    ]
    root = models.Session(session_id="root_seg", title="root", messages=root_messages)
    root.pre_compression_snapshot = True
    root.save(skip_index=True)
    child = models.Session(session_id="child_seg", title="child", messages=child_messages)
    child.parent_session_id = "root_seg"
    child.lineage_parent_overlap_count = 3
    child.pre_compression_snapshot = True
    child.save(skip_index=True)
    tip = models.Session(session_id="tip_seg", title="tip", messages=tip_messages)
    tip.parent_session_id = "child_seg"
    tip.lineage_parent_overlap_count = 3
    tip.anchor_activity_scenes = {
        "tip-scene": {
            "message_index": 4,
            "message_ref": "tip-ref",
            "scene": {"version": "activity_scene_v1"},
        }
    }
    tip.save(skip_index=True)

    monkeypatch.setattr(
        models.Session,
        "load",
        classmethod(lambda cls, sid: (_ for _ in ()).throw(AssertionError("full load forbidden"))),
    )
    is_renderable = routes._message_counts_as_renderable_for_window
    tail = models.read_indexed_session_lineage_window(
        "tip_seg", visible_limit=3, is_renderable=is_renderable
    )
    older = models.read_indexed_session_lineage_window(
        "tip_seg",
        visible_limit=2,
        msg_before=tail["messages_offset"],
        is_renderable=is_renderable,
    )

    logical = root_messages + child_messages[3:] + tip_messages[3:]
    expected_tail, expected_offset = routes._message_window_for_display(logical, msg_limit=3)
    expected_older, expected_older_offset = routes._message_window_for_display(
        logical, msg_limit=2, msg_before=expected_offset
    )
    assert tail["messages"] == expected_tail
    assert tail["message_count"] == len(logical)
    assert tail["messages_offset"] == expected_offset
    assert tail["lineage_segment_count"] == 3
    assert tail["anchor_activity_scenes"]["tip-scene"]["message_index"] == len(logical) - 1
    assert older["messages"] == expected_older
    assert older["messages_offset"] == expected_older_offset
