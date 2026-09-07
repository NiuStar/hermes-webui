"""Lossless disk-only encoding: live/recovery payloads stay identical."""
import copy
import json

import pytest

from api import run_journal as journal


@pytest.mark.parametrize("name", ["tool", "tool_complete", "interim_assistant", "reasoning", "token"])
def test_large_process_payload_is_smaller_on_disk_and_replays_losslessly(tmp_path, name):
    payload = {"text": "进度 and repeated output\n" * 2000, "args": {"command": "x" * 20000}, "tid": "call-1"}
    original = copy.deepcopy(payload)
    writer = journal.RunJournalWriter("session", "run", session_dir=tmp_path)
    first = writer.append_sse_event("start", {"session_id": "session"})
    event = writer.append_sse_event(name, payload)
    path = tmp_path / "_run_journal/session/run.jsonl"
    assert path.stat().st_size < len(json.dumps(payload).encode()) // 4
    assert event["payload"] == original == payload
    assert journal.read_run_events("session", "run", session_dir=tmp_path)["events"][-1] == event
    replay = journal.read_session_run_events("session", after_event_id=first["event_id"], session_dir=tmp_path)
    assert replay["status"] == "ok"
    assert replay["events"] == [event]
    # No writer memory is needed after restart; sequence seeding reads encoded rows.
    journal._SEQ_CACHE.pop(str(path), None)
    restarted = journal.RunJournalWriter("session", "run", session_dir=tmp_path)
    assert restarted.append_sse_event("token", {"text": "tail"})["seq"] == 3


@pytest.mark.parametrize("name,payload", [
    ("done", {"ephemeral": True, "answer": "btw answer" * 2000}),
    ("apperror", {"type": "interrupted", "partial": "work" * 5000}),
    ("cancel", {"text": "partial" * 5000}),
    ("stream_end", {"text": "end" * 5000}),
    ("future_event", {"text": "future" * 5000}),
    ("token", {"text": "small"}),
    ("tool", {"ephemeral": True, "args": {"text": "x" * 20000}}),
])
def test_unselected_ephemeral_and_terminal_events_keep_existing_format(tmp_path, name, payload):
    event = journal.RunJournalWriter("session", "run", session_dir=tmp_path).append_sse_event(name, payload)
    raw = json.loads((tmp_path / "_run_journal/session/run.jsonl").read_text())
    assert raw == event
    assert raw["payload"] == payload


@pytest.mark.parametrize("corruption", ["base64", "truncated", "trailing", "oversized", "unknown"])
def test_corrupt_encoding_fails_closed_and_does_not_hide_following_rows(tmp_path, monkeypatch, corruption):
    writer = journal.RunJournalWriter("session", "run", session_dir=tmp_path)
    writer.append_sse_event("start", {})
    writer.append_sse_event("tool", {"args": {"content": "abc" * 10000}})
    writer.append_sse_event("token", {"text": "tail"})
    path = tmp_path / "_run_journal/session/run.jsonl"
    lines = path.read_text().splitlines()
    row = json.loads(lines[1])
    assert row["payload_encoding"] == "zlib-base64-json-v1"
    if corruption == "base64":
        row["payload"] = "not valid base64!"
    elif corruption == "truncated":
        row["payload"] = row["payload"][:-4]
    elif corruption == "trailing":
        row["payload"] = journal.base64.b64encode(journal.base64.b64decode(row["payload"]) + b"extra").decode()
    elif corruption == "oversized":
        monkeypatch.setattr(journal, "_PAYLOAD_DECODE_MAX_BYTES", 100)
    else:
        row["payload_encoding"] = "future-v2"
    lines[1] = json.dumps(row)
    path.write_text("\n".join(lines) + "\n")
    recovered = journal.read_run_events("session", "run", session_dir=tmp_path)
    assert [row["seq"] for row in recovered["events"]] == [1, 3]
    assert len(recovered["malformed"]) == 1
    assert journal.read_session_run_events("session", after_event_id="run:1", session_dir=tmp_path)["status"] == "replay_malformed"


def test_replay_budget_counts_expanded_payloads_across_rows(tmp_path):
    writer = journal.RunJournalWriter("session", "run", session_dir=tmp_path)
    writer.append_sse_event("start", {})
    for _ in range(3):
        writer.append_sse_event("tool", {"args": {"content": "x" * 10000}})
    assert (tmp_path / "_run_journal/session/run.jsonl").stat().st_size < 5000
    replay = journal.read_session_run_events(
        "session", after_event_id="run:1", session_dir=tmp_path, max_bytes=25000,
    )
    assert replay["status"] == "replay_limit_bytes"
    assert replay["events"] == []


def test_write_disable_retains_existing_encoded_replay(tmp_path, monkeypatch):
    payload = {"text": "progress" * 2000}
    writer = journal.RunJournalWriter("session", "run", session_dir=tmp_path)
    first = writer.append_sse_event("token", payload)
    monkeypatch.setenv("HERMES_WEBUI_JOURNAL_COMPRESSION", "0")
    writer.append_sse_event("token", payload)
    rows = [json.loads(line) for line in (tmp_path / "_run_journal/session/run.jsonl").read_text().splitlines()]
    assert "payload_encoding" in rows[0]
    assert "payload_encoding" not in rows[1]
    assert journal.read_run_events("session", "run", session_dir=tmp_path)["events"][0] == first


def test_payload_above_decode_ceiling_remains_raw(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "_PAYLOAD_DECODE_MAX_BYTES", 5000)
    payload = {"text": "x" * 6000}
    journal.RunJournalWriter("session", "run", session_dir=tmp_path).append_sse_event("token", payload)
    raw = json.loads((tmp_path / "_run_journal/session/run.jsonl").read_text())
    assert "payload_encoding" not in raw
    assert raw["payload"] == payload


def test_encoded_crash_progress_reaches_visible_and_model_context(tmp_path, monkeypatch):
    from api.models import Session, _append_journaled_partial_output
    from api.streaming import _context_messages_for_new_turn

    monkeypatch.setattr(journal, "_default_session_dir", lambda: tmp_path)
    text = "Verified progress before restart.\n" * 400
    journal.RunJournalWriter("session", "run").append_sse_event("token", {"text": text})
    raw = json.loads((tmp_path / "_run_journal/session/run.jsonl").read_text())
    assert raw["payload_encoding"] == "zlib-base64-json-v1"
    session = Session(session_id="session", title="recovery", messages=[], context_messages=[])
    assert _append_journaled_partial_output(session, "run", dedupe_existing=True)
    assert text.strip() in "\n".join(m.get("content", "") for m in session.messages)
    context = _context_messages_for_new_turn(session, "Continue")
    assert text.strip() in "\n".join(m.get("content", "") for m in context)
