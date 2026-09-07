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
