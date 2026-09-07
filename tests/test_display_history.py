"""Isolated history projection tests; never enable a production scope."""
import importlib.util
import sqlite3
import pytest


def test_completed_generation_rejects_late_rows_and_rolls_back_partial_build(tmp_path):
    from api.display_history import HistoryStore, ScopeKey
    store = HistoryStore(tmp_path / "history.sqlite")
    scope = ScopeKey("profile", "session")
    generation = store.build_shadow(scope, [{"role": "user", "content": "ok"}], max_bytes=1000)
    with store._connect() as db:
        with pytest.raises(sqlite3.IntegrityError, match="sealed_history"):
            db.execute("INSERT INTO shadow_rows VALUES(?,?,?,?,?,?)",
                       (scope.key, generation, 1, 1, 2, '{}'))
    # A real SQL fault after one row must not expose a partial generation.
    with store._connect() as db:
        db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON shadow_rows WHEN NEW.pos=1 BEGIN SELECT RAISE(ABORT,'injected_failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected_failure"):
        store.build_shadow(scope, [{"role": "user"}, {"role": "assistant"}], max_bytes=1000)
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM shadow_generations").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM shadow_rows").fetchone()[0] == 1
    assert store.page_shadow(scope, generation, limit=1)["messages"][0]["content"] == "ok"


def test_scene_hydration_matches_legacy_get_for_each_page(tmp_path):
    from api.display_history import HistoryStore, ScopeKey
    from tests.test_session_tail_payload import _FakeSession, _invoke
    from api.routes import _assistant_anchor_scene_message_ref
    session = _FakeSession([
        {"role": "user", "content": "question", "timestamp": 1},
        {"role": "assistant", "content": "answer", "timestamp": 2},
        {"role": "user", "content": "question two", "timestamp": 3},
        {"role": "assistant", "content": "answer two", "timestamp": 4},
    ])
    ref = _assistant_anchor_scene_message_ref(session.messages[1])
    session.anchor_activity_scenes = {ref: {
        "message_ref": ref, "message_index": 1, "stream_id": "settled-run",
        "scene": {"schema": "activity_scene_v1", "items": [], "final_text": "answer"},
    }}
    store = HistoryStore(tmp_path / "history.sqlite")
    scope = ScopeKey("profile", session.session_id)
    assert "scenes" in __import__('inspect').signature(store.build_shadow).parameters, "scene capture missing"
    generation = store.build_shadow(scope, session.messages, tool_calls=session.tool_calls,
                                    scenes=session.anchor_activity_scenes, max_bytes=10000)
    for before in (None, 2):
        query = "session_id=tail_payload_001&resolve_model=0&msg_limit=2"
        if before is not None:
            query += f"&msg_before={before}"
        oracle = _invoke(session, query=query)
        page = store.page_shadow(scope, generation, limit=2, before=before)
        assert page["messages"] == oracle["messages"]


@pytest.mark.parametrize("messages", [
    [],
    [{"role": "tool", "content": "x" * 5000, "tool_call_id": "orphan"}],
    [{"role": "assistant", "content": "call", "tool_calls": [{"id": "t"}]},
     {"role": "tool", "content": "x" * 5000, "tool_call_id": "t"},
     {"role": "tool", "content": "orphan", "tool_call_id": "other"}],
    [{"role": "user", "content": "duplicate", "active": 0},
     {"role": "user", "content": "duplicate", "active": 1},
     {"role": "assistant", "content": "answer"}],
])
def test_indexed_windows_match_get_edge_cases(tmp_path, messages):
    from api.display_history import HistoryStore, ScopeKey
    from tests.test_session_tail_payload import _FakeSession, _invoke
    session = _FakeSession(messages)
    session.tool_calls = []
    store = HistoryStore(tmp_path / "history.sqlite")
    scope = ScopeKey("profile", session.session_id)
    generation = store.build_shadow(scope, messages, max_bytes=20000)
    for limit in (1, 2, 5):
        for before in (None, 0, 1, len(messages), 999):
            query = f"session_id=tail_payload_001&resolve_model=0&msg_limit={limit}"
            if before is not None:
                query += f"&msg_before={before}"
            oracle = _invoke(session, query=query)
            page = store.page_shadow(scope, generation, limit=limit, before=before)
            assert page == {k: oracle[k] for k in page}


def test_unsealed_generation_is_not_readable(tmp_path):
    from api.display_history import HistoryStore, ScopeKey
    store = HistoryStore(tmp_path / "history.sqlite")
    scope = ScopeKey("profile", "session")
    with store._connect() as db:
        db.execute("INSERT INTO shadow_generations VALUES(?,?,?)", (scope.key, "partial", 5))
    with pytest.raises(KeyError):
        store.page_shadow(scope, "partial", limit=1)


def test_scope_isolation_immutability_budget_and_indexed_read(tmp_path):
    from api.display_history import HistoryStore, ScopeKey
    store = HistoryStore(tmp_path / "history.sqlite")
    scope = ScopeKey("profile-a", "same-id")
    generation = store.build_shadow(scope, [{"role": "user", "content": str(i)} for i in range(1000)], max_bytes=100000)
    with pytest.raises(KeyError):
        store.page_shadow(ScopeKey("profile-b", "same-id"), generation, limit=1)
    with pytest.raises(ValueError, match="budget"):
        store.build_shadow(scope, [{"role": "user", "content": "too large"}], max_bytes=1)
    with store._connect() as db:
        for statement in ("UPDATE shadow_rows SET payload='{}'", "DELETE FROM shadow_rows",
                          "DELETE FROM shadow_seals", "UPDATE shadow_generations SET total=0"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable_history"):
                db.execute(statement)
        plan = db.execute("EXPLAIN QUERY PLAN SELECT pos,tail_end FROM shadow_rows WHERE scope=? AND generation=? AND renderable=1 AND pos<? ORDER BY pos DESC LIMIT ?", (scope.key, generation, 1000, 2)).fetchall()
        assert any("shadow_visible" in str(row) and "SEARCH" in str(row) for row in plan)
    assert store.page_shadow(scope, generation, limit=2)["_messages_offset"] == 998


def test_history_survives_reopen_and_matches_real_legacy_get(tmp_path):
    assert importlib.util.find_spec("api.display_history") is not None, "history builder missing"
    from api.display_history import HistoryStore, ScopeKey
    from tests.test_session_tail_payload import _FakeSession, _invoke

    session = _FakeSession([
        {"role": "user", "content": "first", "timestamp": 1},
        {"role": "assistant", "content": "answer", "timestamp": 2},
        {"role": "user", "content": "next", "timestamp": 3},
        {"role": "assistant", "content": "last", "timestamp": 4},
    ])
    scope = ScopeKey("isolated-profile-identity", session.session_id)
    path = tmp_path / "history.sqlite"
    store = HistoryStore(path)
    generation = store.build_shadow(scope, session.messages, tool_calls=session.tool_calls,
                                    max_bytes=1_000_000)
    store = HistoryStore(path)
    for before in (None, 2, 0):
        query = "session_id=tail_payload_001&messages=1&resolve_model=0&msg_limit=2"
        if before is not None:
            query += f"&msg_before={before}"
        oracle = _invoke(session, query=query)
        page = store.page_shadow(scope, generation, limit=2, before=before)
        for key in ("messages", "tool_calls", "_messages_offset", "_messages_truncated", "message_count"):
            assert page[key] == oracle[key], key
    assert store.read_candidate(scope, limit=2) is None  # unproven writers stay LEGACY
