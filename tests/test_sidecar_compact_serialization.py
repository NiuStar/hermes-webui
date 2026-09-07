"""Whitespace-only sidecar reduction; no transcript/context or storage migration."""
import copy
import json

import pytest

import api.models as M


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """Model-only tests need no HTTP server or extra test deployment."""
    yield


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(M, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    monkeypatch.setattr(M, "_clear_webui_zero_message_orphan_tombstone", lambda sid: None)
    monkeypatch.setattr(M, "_clear_webui_deleted_session_tombstone", lambda sid: None)
    return tmp_path


def make_session(store, count):
    text = '中文 🙂\n  whitespace stays: "messages": [ ] \\ end'
    messages = [{"role": "user" if i % 2 == 0 else "assistant",
                 "content": [{"type": "text", "text": text}], "id": f"m{i}",
                 "timestamp": 1000 + i, "provider_metadata": {"nested": [None, True, 2.5]},
                 "tool_calls": [{"id": f"t{i}", "type": "function", "function": {
                     "name": "read_file", "arguments": '{ "path": "文件" }'}}]}
                for i in range(count)]
    session = M.Session(
        session_id="compact-test", title=text, workspace=str(store), model="test-model",
        created_at=1000, updated_at=2000, profile="default", pinned=True,
        messages=messages, context_messages=[{"role": "system", "content": text}] + copy.deepcopy(messages),
        tool_calls=[{"id": "t0", "name": "read_file", "args": {"path": "文件"},
                     "result": text, "truncated": True, "original_size": 9000}],
        truncation_watermark=1200, truncation_boundary={"message_id": "m0", "visible_idx": 1},
        clear_generation=3, intentional_shrink_generation=4,
        compression_anchor_summary=text, compression_anchor_visible_idx=1,
        compression_anchor_details={"messages": ["nested key is not the boundary"]},
        context_engine_state={"nested": {"anchor_activity_scenes": []}},
        anchor_activity_scenes={"scene0": {"updated_at": 1500, "scene": {
            "activity_rows": [{"kind": "tool", "text": text, "tool": {"name": "read_file"}}]}}},
    )
    session.context_messages.append({"role": "tool", "tool_call_id": "t0", "name": "read_file",
                                     "content": text, "is_error": False})
    session.last_usage = {"input_tokens": 42, "details": [1, None, False]}
    return session


@pytest.mark.parametrize("count", [0, 1, 200])
def test_roundtrip_and_byte_reduction(store, count):
    session = make_session(store, count)
    before = copy.deepcopy(session.__dict__)
    session.save(touch_updated_at=False, skip_index=True)
    raw = session.path.read_bytes()
    data = json.loads(raw)
    pretty = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    # Exact old-format prefix, including indentation/order; body whitespace only.
    marker = b'\n  "messages": '
    assert raw.split(marker, 1)[0] == pretty.split(marker, 1)[0]
    assert len(raw) < len(pretty)
    print(f"sidecar count={count}: pretty={len(pretty)} compact={len(raw)} "
          f"saved={len(pretty)-len(raw)} ({100*(1-len(raw)/len(pretty)):.2f}%)")
    loaded = M.Session.load(session.session_id)
    for key in ("messages", "context_messages", "tool_calls", "anchor_activity_scenes",
                "truncation_watermark", "truncation_boundary", "clear_generation",
                "intentional_shrink_generation", "compression_anchor_summary",
                "compression_anchor_details", "context_engine_state", "updated_at", "title"):
        assert data[key] == before[key]
        assert getattr(loaded, key) == before[key]
        assert getattr(session, key) == before[key]
    # Extra persisted fields retain exactly the same JSON value (the constructor
    # does not currently restore arbitrary extras such as last_usage).
    assert data["last_usage"] == before["last_usage"]
    loaded.save(touch_updated_at=False, skip_index=True)
    assert M.Session.load(session.session_id).context_messages == before["context_messages"]


def test_prefix_readers_never_full_load_large_body(store, monkeypatch):
    session = make_session(store, 200)
    session.save(touch_updated_at=False, skip_index=True)
    assert session.path.stat().st_size > 65536
    raw = session.path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("metadata reader fell back to full load")

    monkeypatch.setattr(M.Session, "load", forbidden)
    prefix = M._persisted_session_meta_prefix(session.session_id)
    assert prefix["message_count"] == 200
    assert prefix["anchor_scene_index"] == {"scene0": 1500}
    assert "messages" not in prefix and "anchor_activity_scenes" not in prefix
    assert M._persisted_message_count(session.session_id) == 200
    stub = M.Session.load_metadata_only(session.session_id)
    assert stub._loaded_metadata_only and stub._metadata_message_count == 200
    assert stub.messages == stub.context_messages == stub.tool_calls == []
    assert stub.title == session.title and stub.updated_at == 2000
    assert stub.truncation_boundary == session.truncation_boundary
    with pytest.raises(RuntimeError, match="metadata-only"):
        stub.save()
    assert session.path.read_bytes() == raw


@pytest.mark.parametrize("old_pretty", [False, True])
def test_shrink_backup_preserves_exact_previous_file(store, old_pretty):
    session = make_session(store, 4)
    session.save(touch_updated_at=False, skip_index=True)
    if old_pretty:
        session.path.write_text(json.dumps(json.loads(session.path.read_bytes()),
                                          ensure_ascii=False, indent=2), encoding="utf-8")
    previous = session.path.read_bytes()
    loaded = M.Session.load(session.session_id)
    loaded.messages = loaded.messages[:1]
    loaded.save(touch_updated_at=False, skip_index=True)
    assert session.path.with_suffix(".json.bak").read_bytes() == previous
    assert M.Session.load(session.session_id).messages == session.messages[:1]
    assert M._persisted_message_count(session.session_id) == 1


def test_failed_atomic_replace_keeps_previous_file(store, monkeypatch):
    session = make_session(store, 1)
    session.save(touch_updated_at=False, skip_index=True)
    previous = session.path.read_bytes()
    session.messages.append({"role": "assistant", "content": "new"})

    def fail_replace(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(M, "_safe_replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        session.save(touch_updated_at=False, skip_index=True)
    assert session.path.read_bytes() == previous
    assert not list(store.glob("*.tmp.*"))
