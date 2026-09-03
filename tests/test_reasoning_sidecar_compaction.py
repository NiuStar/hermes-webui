import copy
import json
from unittest.mock import patch

import api.models as models
from api.routes import _message_window_for_display, _messages_for_limited_payload
from api.streaming import _merge_display_messages_after_agent_result


def _reasoning(*, message_id=None, timestamp=100.0, text="same reasoning"):
    message = {
        "role": "assistant",
        "content": "",
        "reasoning": text,
        "timestamp": timestamp,
    }
    if message_id is not None:
        message["id"] = message_id
    return message


def test_reasoning_replay_dedupes_stable_and_legacy_rows_without_losing_answers():
    visible_answer = {
        "id": 77,
        "role": "assistant",
        "content": "final answer must survive",
        "reasoning": "private trace",
        "timestamp": 200.0,
    }
    rows = [
        {"role": "user", "content": "question", "timestamp": 90.0},
        *[copy.deepcopy(_reasoning()) for _ in range(32)],
        *[copy.deepcopy(_reasoning(message_id=77, timestamp=150.0)) for _ in range(4)],
        visible_answer,
    ]

    deduped = models._dedupe_persisted_reasoning_rows(rows)

    assert len(deduped) == 4
    assert sum(message.get("reasoning") == "same reasoning" for message in deduped) == 2
    assert any(message.get("content") == "final answer must survive" for message in deduped)
    assert deduped[-1]["content"] == "final answer must survive"


def test_reasoning_replay_cannot_grow_the_display_transcript_each_turn():
    history = [
        {"role": "user", "content": "question", "timestamp": 90.0},
        _reasoning(),
        {"role": "assistant", "content": "final answer", "timestamp": 200.0},
    ]
    context = list(history)

    for turn in range(12):
        replayed = list(context) + [
            copy.deepcopy(_reasoning())
            for _ in range(2 ** min(turn, 8))
        ]
        history = _merge_display_messages_after_agent_result(
            history,
            context,
            replayed,
            "",
        )
        assert sum(
            message.get("role") == "assistant"
            and not message.get("content")
            and message.get("reasoning") == "same reasoning"
            for message in history
        ) == 1

    assert any(message.get("content") == "final answer" for message in history)


def test_sidebar_time_ignores_old_reasoning_rows_appended_after_real_activity():
    messages = [
        {"role": "user", "content": "new question", "timestamp": 300.0},
        {"role": "assistant", "content": "new final answer", "timestamp": 301.0},
        _reasoning(timestamp=120.0, text="old replayed reasoning"),
        _reasoning(timestamp=120.0, text="old replayed reasoning"),
    ]

    assert models._last_message_timestamp(messages) == 301.0


def test_sidebar_time_uses_latest_real_activity_in_replayed_nonmonotonic_history():
    messages = [
        {"role": "user", "content": "older question", "timestamp": 100.0},
        {"role": "assistant", "content": "newest final answer", "timestamp": 400.0},
        {"role": "assistant", "content": "replayed older answer", "timestamp": 250.0},
    ]

    assert models._last_message_timestamp(messages) == 400.0


def test_session_save_indexes_real_activity_time_without_shrinking_history(
    tmp_path,
    monkeypatch,
):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    models.SESSIONS.clear()

    session = models.Session(
        session_id="reasoning-save",
        messages=[
            {"role": "user", "content": "new question", "timestamp": 300.0},
            {"role": "assistant", "content": "new final answer", "timestamp": 301.0},
            *[copy.deepcopy(_reasoning(timestamp=120.0)) for _ in range(16)],
        ],
    )
    session.save()

    persisted = json.loads((session_dir / "reasoning-save.json").read_text(encoding="utf-8"))
    index = json.loads(index_file.read_text(encoding="utf-8"))

    assert len(persisted["messages"]) == 18
    assert index[0]["last_message_at"] == 301.0


def test_reasoning_only_tail_does_not_hide_latest_answer_from_default_page():
    messages = [
        {"role": "user", "content": "question", "timestamp": 100.0},
        {"role": "assistant", "content": "final answer", "timestamp": 101.0},
        *[copy.deepcopy(_reasoning(timestamp=50.0)) for _ in range(40)],
    ]

    window, offset = _message_window_for_display(messages, msg_limit=30)
    payload = _messages_for_limited_payload(window)

    assert offset == 0
    assert payload[-1]["content"] == "final answer"
    assert all(not models._is_reasoning_only_assistant_message(row) for row in payload)


def test_idle_sidebar_row_refreshes_from_clearly_newer_sidecar_metadata(
    tmp_path,
    monkeypatch,
):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    models.SESSIONS.clear()

    session = models.Session(
        session_id="idle-stale-index",
        title="Existing session",
        messages=[
            {"role": "user", "content": "old", "timestamp": 100.0},
            {"role": "assistant", "content": "new answer", "timestamp": 102.0},
        ],
        updated_at=102.0,
    )
    session.save(touch_updated_at=False)
    index_file.write_text(json.dumps([{
        "session_id": session.session_id,
        "title": session.title,
        "message_count": 1,
        "user_message_count": 1,
        "created_at": 100.0,
        "updated_at": 100.0,
        "last_message_at": 100.0,
        "pinned": False,
        "archived": False,
    }]), encoding="utf-8")
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda _rows: None)

    with patch.object(models.Session, "load", side_effect=AssertionError("full load not allowed")):
        rows = models.all_sessions()

    assert rows[0]["message_count"] == 2
    assert rows[0]["last_message_at"] == 102.0


def test_metadata_only_session_load_skips_full_regeneration_authority(monkeypatch):
    """The first-paint metadata request must not hash the full transcript."""
    from types import SimpleNamespace
    from urllib.parse import urlparse

    import api.routes as routes

    session = SimpleNamespace(
        session_id="metadata-fast-path",
        profile="default",
        active_stream_id=None,
        pending_user_message=None,
        pending_attachments=[],
        pending_started_at=None,
        pending_user_source=None,
        context_length=0,
        threshold_tokens=0,
        last_prompt_tokens=0,
        tool_calls=[],
        read_only=False,
        is_cli_session=False,
        source_tag="webui",
        raw_source="webui",
        session_source="webui",
        compact=lambda **_kwargs: {
            "session_id": "metadata-fast-path",
            "profile": "default",
            "message_count": 20000,
            "last_message_at": 123.0,
            "updated_at": 123.0,
            "read_only": False,
            "is_cli_session": False,
            "source_tag": "webui",
            "raw_source": "webui",
            "session_source": "webui",
        },
    )
    monkeypatch.setattr(routes, "get_session", lambda _sid, metadata_only=False: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda _session: None)
    monkeypatch.setattr(routes, "_session_requires_cli_metadata_lookup", lambda _session: False)
    monkeypatch.setattr(
        routes,
        "_metadata_only_message_summary",
        lambda *_args, **_kwargs: {"message_count": 20000, "last_message_at": 123.0},
    )
    monkeypatch.setattr(
        routes,
        "_resolve_context_length_for_session_model",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(routes, "_pre_compression_continuation_session_id", lambda _session: None)
    monkeypatch.setattr(routes, "redact_session_data", lambda payload: payload)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200: payload)

    import api.session_ops as session_ops

    monkeypatch.setattr(
        session_ops,
        "regeneration_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("metadata-only request hashed the full transcript")
        ),
    )

    result = routes.handle_get(
        SimpleNamespace(_safe_webui_print=lambda *_args: None),
        urlparse(
            "/api/session?session_id=metadata-fast-path&messages=0&resolve_model=0"
        ),
    )

    assert result["session"]["message_count"] == 20000
    assert "regeneration_revision" not in result["session"]
