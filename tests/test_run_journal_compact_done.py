import json
import io

from api.models import Session
from api.run_journal import (
    RunJournalWriter,
    read_run_events,
    read_session_run_events,
)


def _sse_payload(body: str, event_name: str):
    lines = body.splitlines()
    event_index = lines.index(f"event: {event_name}")
    return json.loads(lines[event_index + 1].removeprefix("data: "))


def test_writer_compacts_done_payload_without_changing_other_events(tmp_path):
    writer = RunJournalWriter("session_1", "run_1", session_dir=tmp_path)
    token_payload = {"text": "hello"}
    done_payload = {
        "session": {
            "session_id": "session_1",
            "message_count": 20_000,
            "updated_at": 123.0,
            "messages": [
                {"role": "assistant", "content": "x" * 4096}
                for _ in range(200)
            ],
            "tool_calls": [{"name": "large", "args": "y" * 4096}],
            "regeneration_revision": "revision-1",
        },
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "terminal_state": "tool_limit_reached",
        "terminal_reason": "max_iterations",
    }

    writer.append_sse_event("token", token_payload)
    writer.append_sse_event("done", done_payload)

    events = read_run_events("session_1", "run_1", session_dir=tmp_path)["events"]
    assert events[0]["payload"] == token_payload
    compact = events[1]["payload"]
    assert compact["_journal_compact_done"] == 1
    assert compact["session"] == {
        "session_id": "session_1",
        "message_count": 20_000,
        "updated_at": 123.0,
        "regeneration_revision": "revision-1",
    }
    assert compact["usage"] == done_payload["usage"]
    assert compact["terminal_state"] == "tool_limit_reached"
    assert compact["terminal_reason"] == "max_iterations"
    assert "messages" not in compact["session"]
    assert "tool_calls" not in compact["session"]
    assert len(json.dumps(compact)) < 4096
    assert len(done_payload["session"]["messages"]) == 200


def test_writer_keeps_apperror_recovery_session_payload(tmp_path):
    writer = RunJournalWriter("session_1", "run_1", session_dir=tmp_path)
    recovery = {
        "type": "persistence_error",
        "session": {
            "session_id": "session_1",
            "messages": [{"role": "assistant", "content": "recover me"}],
        },
    }

    writer.append_sse_event("apperror", recovery)

    event = read_run_events("session_1", "run_1", session_dir=tmp_path)["events"][0]
    assert event["payload"] == recovery


def test_writer_keeps_ephemeral_done_as_its_only_recovery_copy(tmp_path):
    writer = RunJournalWriter("session_1", "run_1", session_dir=tmp_path)
    ephemeral = {
        "session": {
            "session_id": "session_1",
            "message_count": 1,
            "messages": [{"role": "assistant", "content": "temporary answer"}],
        },
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "ephemeral": True,
        "answer": "temporary answer",
    }

    writer.append_sse_event("done", ephemeral)

    event = read_run_events("session_1", "run_1", session_dir=tmp_path)["events"][0]
    assert event["payload"] == ephemeral
    assert event["payload"]["session"]["messages"][0]["content"] == "temporary answer"


def test_dead_stream_replay_rehydrates_compact_done_from_authoritative_session(
    monkeypatch,
):
    import api.routes as routes

    session = Session(
        session_id="session_1",
        title="Settled",
        messages=[
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "final answer"},
        ],
    )
    handler = type("Handler", (), {"wfile": io.BytesIO()})()
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda run_id: {"session_id": "session_1", "run_id": run_id, "terminal": True},
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda *_args, **_kwargs: {
            "events": [
                {
                    "event": "done",
                    "event_id": "run_1:2",
                    "payload": {
                        "_journal_compact_done": 1,
                        "session": {
                            "session_id": "session_1",
                            "message_count": 2,
                        },
                        "usage": {"output_tokens": 3},
                    },
                }
            ]
        },
    )
    monkeypatch.setattr(routes, "get_session", lambda sid: session)

    assert routes._replay_run_journal(handler, "run_1", 0) is True

    body = handler.wfile.getvalue().decode("utf-8")
    assert "event: done\n" in body
    replayed = _sse_payload(body, "done")
    assert replayed["session"]["messages"][-1]["content"] == "final answer"
    assert replayed["usage"]["output_tokens"] == 3
    assert "_journal_compact_done" not in body


def test_dead_stream_replay_never_emits_incomplete_compact_done(monkeypatch):
    import api.routes as routes

    handler = type("Handler", (), {"wfile": io.BytesIO()})()
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda run_id: {"session_id": "session_1", "run_id": run_id, "terminal": True},
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda *_args, **_kwargs: {
            "events": [
                {
                    "event": "done",
                    "event_id": "run_1:2",
                    "payload": {
                        "_journal_compact_done": 1,
                        "session": {"session_id": "session_1", "message_count": 2},
                    },
                }
            ]
        },
    )
    monkeypatch.setattr(routes, "get_session", lambda _sid: (_ for _ in ()).throw(KeyError("missing")))

    assert routes._replay_run_journal(handler, "run_1", 0) is True

    body = handler.wfile.getvalue().decode("utf-8")
    assert "event: done\n" not in body
    assert "event: apperror\n" in body
    assert _sse_payload(body, "apperror")["recovery_control"] is True


def test_dead_stream_replay_rejects_compact_done_when_session_advanced(monkeypatch):
    import api.routes as routes

    advanced = Session(
        session_id="session_1",
        title="Advanced",
        messages=[
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "new question"},
            {"role": "assistant", "content": "new answer"},
        ],
    )
    handler = type("Handler", (), {"wfile": io.BytesIO()})()
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda run_id: {"session_id": "session_1", "run_id": run_id, "terminal": True},
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda *_args, **_kwargs: {
            "events": [
                {
                    "event": "done",
                    "event_id": "run_1:2",
                    "payload": {
                        "_journal_compact_done": 1,
                        "session": {"session_id": "session_1", "message_count": 2},
                    },
                }
            ]
        },
    )
    monkeypatch.setattr(routes, "get_session", lambda _sid: advanced)

    assert routes._replay_run_journal(handler, "run_1", 0) is True

    body = handler.wfile.getvalue().decode("utf-8")
    assert "event: done\n" not in body
    assert _sse_payload(body, "apperror")["recovery_control"] is True


def test_session_scoped_replay_requests_snapshot_for_compact_done(tmp_path):
    writer = RunJournalWriter("session_1", "run_1", session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "answer"})
    writer.append_sse_event(
        "done",
        {
            "session": {
                "session_id": "session_1",
                "message_count": 2,
                "messages": [{"role": "assistant", "content": "answer"}],
            }
        },
    )
    writer.append_sse_event("stream_end", {"session_id": "session_1"})

    replay = read_session_run_events(
        "session_1", after_event_id="run_1:1", session_dir=tmp_path
    )

    assert replay["status"] == "replay_snapshot_required"
    assert replay["events"] == []


def test_session_scoped_replay_after_compact_done_keeps_stream_end(tmp_path):
    writer = RunJournalWriter("session_1", "run_1", session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "answer"})
    writer.append_sse_event(
        "done",
        {
            "session": {
                "session_id": "session_1",
                "message_count": 2,
                "messages": [{"role": "assistant", "content": "answer"}],
            }
        },
    )
    writer.append_sse_event("stream_end", {"session_id": "session_1"})

    replay = read_session_run_events(
        "session_1", after_event_id="run_1:2", session_dir=tmp_path
    )

    assert replay["status"] == "ok"
    assert [event["event"] for event in replay["events"]] == ["stream_end"]