"""Regression coverage for empty active/pending session save writebacks."""

import json
from pathlib import Path

import api.config as config
import api.models as models
from api.models import Session


def test_empty_active_pending_save_cannot_overwrite_existing_messages(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    models.SESSIONS.clear()

    sid = "pending_overwrite_guard"
    existing = Session(
        session_id=sid,
        messages=[
            {"role": "user", "content": "prompt"},
            {"role": "assistant", "content": "answer"},
        ],
    )
    existing.save()

    stale = Session(
        session_id=sid,
        messages=[],
        active_stream_id="stale-stream",
        pending_user_message="prompt",
        pending_started_at=123.0,
    )
    stale.save()

    persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert [m["content"] for m in persisted["messages"]] == ["prompt", "answer"]
    assert persisted["message_count"] == 2

    index = json.loads(index_file.read_text(encoding="utf-8"))
    indexed = next(row for row in index if row["session_id"] == sid)
    assert indexed["message_count"] == 2


def test_save_streams_existing_backup_and_new_message_array(tmp_path, monkeypatch):
    """Large saves must not materialize either full sidecar as one Python string."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    models.SESSIONS.clear()

    sid = "streaming_large_save"
    original = Session(
        session_id=sid,
        messages=[
            {"role": "user", "content": "old prompt"},
            {"role": "assistant", "content": "x" * 200_000},
            {"role": "user", "content": "new prompt"},
        ],
    )
    original.save()
    original_bytes = (session_dir / f"{sid}.json").read_bytes()

    real_read_text = Path.read_text
    real_dumps = json.dumps

    def guarded_read_text(path, *args, **kwargs):
        if Path(path) == session_dir / f"{sid}.json":
            raise AssertionError("save must not read the full existing sidecar")
        return real_read_text(path, *args, **kwargs)

    retained_messages = original.messages[:2]

    def guarded_dumps(value, *args, **kwargs):
        if value is retained_messages:
            raise AssertionError("save must stream the messages array")
        return real_dumps(value, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(json, "dumps", guarded_dumps)

    shrunk = Session(session_id=sid, messages=retained_messages)
    shrunk.save()

    persisted = json.loads((session_dir / f"{sid}.json").read_bytes())
    backup_path = session_dir / f"{sid}.json.bak"
    assert persisted["messages"] == retained_messages
    assert backup_path.read_bytes() == original_bytes


def test_compression_continuation_persists_segment_without_mutating_logical_history(
    tmp_path, monkeypatch
):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    logical_messages = [
        {"role": "user", "content": "old question", "timestamp": 1},
        {"role": "assistant", "content": "old answer", "timestamp": 2},
        {"role": "system", "content": "[CONTEXT COMPACTION] summary", "timestamp": 3},
        {"role": "user", "content": "new question", "timestamp": 4},
        {"role": "assistant", "content": "new answer", "timestamp": 5},
    ]
    session = Session(
        session_id="continuation",
        title="continuation",
        messages=logical_messages,
    )
    session.tool_calls = [
        {"id": "old", "assistant_msg_idx": 1},
        {"id": "new", "assistant_msg_idx": 4},
    ]
    session.anchor_activity_scenes = {
        "old": {"message_index": 1, "scene": {"version": "activity_scene_v1"}},
        "new": {"message_index": 4, "scene": {"version": "activity_scene_v1"}},
    }
    session._persist_message_segment_start = 2

    session.save(skip_index=True)

    payload = json.loads((session_dir / "continuation.json").read_text(encoding="utf-8"))
    assert session.messages == logical_messages
    assert payload["messages"] == logical_messages[2:]
    assert payload["message_count"] == 3
    assert payload["tool_calls"] == [{"id": "new", "assistant_msg_idx": 2}]
    assert payload["anchor_activity_scenes"] == {
        "new": {"message_index": 2, "scene": {"version": "activity_scene_v1"}}
    }
    index = json.loads(
        models._message_offset_index_path("continuation").read_text()
    )
    assert index["message_count"] == 3


def test_invalid_compression_segment_start_fails_closed_to_full_save(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    messages = [{"role": "user", "content": "keep me"}]
    session = Session(session_id="bad_segment", title="bad", messages=messages)
    session._persist_message_segment_start = 99

    session.save(skip_index=True)

    payload = json.loads((session_dir / "bad_segment.json").read_text(encoding="utf-8"))
    assert payload["messages"] == messages


def test_reloaded_segment_advances_logical_count_once_across_repeated_saves(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    logical = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
        {"role": "system", "content": "summary"},
        {"role": "assistant", "content": "tail"},
    ]
    session = Session(session_id="logical_count", messages=logical)
    session.parent_session_id = "parent"
    session.lineage_parent_overlap_count = 1
    session._persist_message_segment_start = 2
    session.save(skip_index=True)

    loaded = Session.load("logical_count")
    assert len(loaded.messages) == 2
    assert loaded.compact()["message_count"] == 4
    loaded.messages.extend([
        {"role": "user", "content": "next"},
        {"role": "assistant", "content": "next answer"},
    ])
    loaded.save(skip_index=True)
    loaded.save(skip_index=True)

    reloaded = Session.load("logical_count")
    assert len(reloaded.messages) == 4
    assert reloaded.lineage_message_count == 6
    assert reloaded.compact()["message_count"] == 6
