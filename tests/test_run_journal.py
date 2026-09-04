import json
import threading
from pathlib import Path

from api.run_journal import (
    RunJournalWriter,
    append_run_event,
    find_run_summary,
    latest_run_summary,
    read_run_events,
    read_session_run_events,
    stale_interrupted_event,
)


def test_run_journal_appends_monotonic_seq_and_reads_after_cursor(tmp_path):
    writer = RunJournalWriter("session_1", "run_1", session_dir=tmp_path)

    first = writer.append_sse_event("token", {"text": "hello"})
    second = writer.append_sse_event("done", {"session": {"session_id": "session_1"}})

    assert first["seq"] == 1
    assert first["event_id"] == "run_1:1"
    assert first["terminal"] is False
    assert second["seq"] == 2
    assert second["terminal"] is True
    assert second["terminal_state"] == "completed"

    journal = read_run_events("session_1", "run_1", after_seq=1, session_dir=tmp_path)
    assert [event["event"] for event in journal["events"]] == ["done"]


def test_run_journal_reads_bounded_replay_window(tmp_path):
    writer = RunJournalWriter("session_1", "run_1", session_dir=tmp_path)

    writer.append_sse_event("token", {"text": "one"})
    writer.append_sse_event("token", {"text": "two"})
    writer.append_sse_event("token", {"text": "three"})
    writer.append_sse_event("token", {"text": "four"})

    journal = read_run_events(
        "session_1",
        "run_1",
        after_seq=1,
        max_seq=3,
        session_dir=tmp_path,
    )

    assert [event["seq"] for event in journal["events"]] == [2, 3]
    assert [event["payload"]["text"] for event in journal["events"]] == ["two", "three"]


def test_run_journal_default_fsyncs_terminal_events_only(tmp_path, monkeypatch):
    path = tmp_path / "_run_journal" / "session_1" / "run_1.jsonl"
    path.parent.mkdir(parents=True)
    path.touch()
    fsync_calls = []
    monkeypatch.delenv("HERMES_WEBUI_RUN_JOURNAL_FSYNC", raising=False)
    monkeypatch.setattr("api.run_journal.os.fsync", lambda fd: fsync_calls.append(fd))

    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)

    assert fsync_calls == []

    append_run_event("session_1", "run_1", "done", {"session": {}}, session_dir=tmp_path)

    assert len(fsync_calls) == 1


def test_run_journal_eager_fsync_mode_fsyncs_non_terminal_events(tmp_path, monkeypatch):
    path = tmp_path / "_run_journal" / "session_1" / "run_1.jsonl"
    path.parent.mkdir(parents=True)
    path.touch()
    fsync_calls = []
    monkeypatch.setenv("HERMES_WEBUI_RUN_JOURNAL_FSYNC", "eager")
    monkeypatch.setattr("api.run_journal.os.fsync", lambda fd: fsync_calls.append(fd))

    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)

    assert len(fsync_calls) == 1


def test_run_journal_tolerates_malformed_lines(tmp_path):
    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)
    path = tmp_path / "_run_journal" / "session_1" / "run_1.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json}\n")
        fh.write(json.dumps(["wrong-shape"]) + "\n")

    journal = read_run_events("session_1", "run_1", session_dir=tmp_path)

    assert len(journal["events"]) == 1
    assert len(journal["malformed"]) == 2


def test_latest_summary_and_find_run_summary_classify_terminal_state(tmp_path):
    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)
    append_run_event("session_1", "run_1", "cancel", {"message": "Cancelled by user"}, session_dir=tmp_path)

    summary = latest_run_summary("session_1", "run_1", session_dir=tmp_path)
    found = find_run_summary("run_1", session_dir=tmp_path)

    assert summary["terminal"] is True
    assert summary["terminal_state"] == "interrupted-by-user"
    assert summary["last_seq"] == 2
    assert found["session_id"] == "session_1"
    assert found["terminal_state"] == "interrupted-by-user"


def test_latest_summary_reuses_unchanged_journal_summary_without_reparsing(tmp_path, monkeypatch):
    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)
    append_run_event("session_1", "run_1", "done", {"session": {}}, session_dir=tmp_path)

    first = latest_run_summary("session_1", "run_1", session_dir=tmp_path)

    monkeypatch.setattr(
        "api.run_journal._read_jsonl",
        lambda _path: (_ for _ in ()).throw(AssertionError("unchanged journal was reparsed")),
    )
    repeated = latest_run_summary("session_1", "run_1", session_dir=tmp_path)

    assert repeated == first


def test_summary_cache_invalidates_on_same_size_rewrite_with_restored_mtime(tmp_path, monkeypatch):
    # A same-inode, same-size rewrite that restores the original mtime_ns (e.g. an
    # atomic replace, or a tool that preserves mtime) must still invalidate the
    # cached summary. The signature includes st_ctime_ns — which advances on any
    # content/metadata change and cannot be forged back — so device/inode/size/
    # mtime collisions alone can never serve a stale summary. Proven at the
    # signature level (the enforced TOCTOU precondition for the cache) with a
    # deterministic stat where ONLY ctime differs.
    import api.run_journal as run_journal

    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)
    path = run_journal._run_path("session_1", "run_1", session_dir=tmp_path)
    real = path.stat()

    class _Stat:
        st_dev = real.st_dev
        st_ino = real.st_ino
        st_size = real.st_size
        st_mtime_ns = real.st_mtime_ns
        st_ctime_ns = real.st_ctime_ns  # overwritten per-call below

    seq = {"ctime": real.st_ctime_ns}

    def fake_stat(self, *a, **k):
        s = _Stat()
        s.st_ctime_ns = seq["ctime"]
        return s

    monkeypatch.setattr(Path, "stat", fake_stat)
    sig_before = run_journal._summary_cache_signature(path)
    # Same dev/inode/size/mtime, but a same-size in-place rewrite advanced ctime.
    seq["ctime"] = real.st_ctime_ns + 1
    sig_after = run_journal._summary_cache_signature(path)

    assert sig_after is not None and sig_before is not None
    assert sig_after != sig_before, "signature must change when only ctime advances"
    assert sig_before[:4] == sig_after[:4], "dev/inode/size/mtime_ns unexpectedly changed"


def test_summary_cache_does_not_store_result_when_journal_changes_during_read(tmp_path, monkeypatch):
    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)
    append_run_event("session_1", "run_1", "done", {"session": {}}, session_dir=tmp_path)

    import api.run_journal as run_journal

    original_read = run_journal._read_jsonl

    def append_after_read(path):
        events, malformed = original_read(path)
        append_run_event(
            "session_1",
            "run_1",
            "cancel",
            {"message": "Cancelled by user"},
            session_dir=tmp_path,
        )
        return events, malformed

    monkeypatch.setattr(run_journal, "_read_jsonl", append_after_read)

    first = latest_run_summary("session_1", "run_1", session_dir=tmp_path)
    second = latest_run_summary("session_1", "run_1", session_dir=tmp_path)

    assert first["terminal_state"] == "completed"
    assert second["terminal_state"] == "interrupted-by-user"



def test_summary_cache_rejects_first_append_that_races_missing_journal_read(tmp_path, monkeypatch):
    import api.run_journal as run_journal

    original_read = run_journal._read_jsonl
    appended = False

    def append_after_missing_read(path):
        nonlocal appended
        events, malformed = original_read(path)
        if not appended:
            appended = True
            append_run_event(
                "session_1",
                "run_first_append",
                "done",
                {"session": {}},
                session_dir=tmp_path,
            )
        return events, malformed

    monkeypatch.setattr(run_journal, "_read_jsonl", append_after_missing_read)

    raced = latest_run_summary("session_1", "run_first_append", session_dir=tmp_path)
    refreshed = latest_run_summary("session_1", "run_first_append", session_dir=tmp_path)

    assert raced["terminal_state"] == "unknown"
    assert refreshed["terminal_state"] == "completed"
    assert refreshed["last_seq"] == 1
    assert refreshed["last_event_id"] == "run_first_append:1"


def test_terminal_state_classification_distinguishes_crash_from_user_cancel(tmp_path):
    append_run_event("session_1", "run_cancelled", "cancel", {"message": "Cancelled by user"}, session_dir=tmp_path)
    append_run_event("session_1", "run_crashed", "apperror", {"type": "interrupted"}, session_dir=tmp_path)
    append_run_event("session_1", "run_failed", "apperror", {"type": "auth_mismatch"}, session_dir=tmp_path)
    append_run_event("session_1", "run_tool_limit", "apperror", {"type": "tool_limit_reached"}, session_dir=tmp_path)
    append_run_event("session_1", "run_tool_limit_done", "done", {"terminal_state": "tool_limit_reached"}, session_dir=tmp_path)
    append_run_event("session_1", "run_unknown_done", "done", {"terminal_state": "future_unknown_state"}, session_dir=tmp_path)
    append_run_event("session_1", "run_done", "done", {"session": {}}, session_dir=tmp_path)

    assert latest_run_summary("session_1", "run_cancelled", session_dir=tmp_path)["terminal_state"] == "interrupted-by-user"
    assert latest_run_summary("session_1", "run_crashed", session_dir=tmp_path)["terminal_state"] == "interrupted-by-crash"
    assert latest_run_summary("session_1", "run_failed", session_dir=tmp_path)["terminal_state"] == "errored"
    assert latest_run_summary("session_1", "run_tool_limit", session_dir=tmp_path)["terminal_state"] == "tool_limit_reached"
    assert latest_run_summary("session_1", "run_tool_limit_done", session_dir=tmp_path)["terminal_state"] == "tool_limit_reached"
    assert latest_run_summary("session_1", "run_unknown_done", session_dir=tmp_path)["terminal_state"] == "completed"
    assert latest_run_summary("session_1", "run_done", session_dir=tmp_path)["terminal_state"] == "completed"


def test_summary_keeps_logical_terminal_state_when_stream_end_follows(tmp_path):
    append_run_event("session_1", "run_1", "apperror", {"type": "auth_mismatch"}, session_dir=tmp_path)
    append_run_event("session_1", "run_1", "stream_end", {"session_id": "session_1"}, session_dir=tmp_path)

    summary = latest_run_summary("session_1", "run_1", session_dir=tmp_path)

    assert summary["terminal"] is True
    assert summary["last_event"] == "stream_end"
    assert summary["terminal_state"] == "errored"


def test_stale_interrupted_event_reports_non_terminal_journal(tmp_path, monkeypatch):
    append_run_event("session_1", "run_1", "token", {"text": "partial"}, session_dir=tmp_path)

    monkeypatch.setattr("api.run_journal._default_session_dir", lambda: tmp_path)
    event = stale_interrupted_event("session_1", "run_1")
    assert event is not None

    assert event["event"] == "apperror"
    assert event["seq"] == 2
    assert event["terminal_state"] == "lost-worker-bookkeeping"
    assert event["payload"]["type"] == "interrupted"
    assert "last journaled event" in event["payload"]["hint"]
    assert "process restarted" not in event["payload"]["message"]
    assert "lost the live worker" not in event["payload"]["message"]
    assert "live worker stopped" in event["payload"]["message"]


def test_stale_interrupted_event_skips_terminal_journal(tmp_path, monkeypatch):
    append_run_event("session_1", "run_1", "done", {"session": {}}, session_dir=tmp_path)

    monkeypatch.setattr("api.run_journal._default_session_dir", lambda: tmp_path)

    assert stale_interrupted_event("session_1", "run_1") is None


def test_closed_run_journal_retention_keeps_latest_count(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUN_JOURNAL_MAX_CLOSED_RUNS", "3")
    monkeypatch.setenv("HERMES_WEBUI_RUN_JOURNAL_MAX_CLOSED_BYTES", str(1024 * 1024))

    for index in range(6):
        append_run_event(
            "session_1",
            f"run_{index}",
            "stream_end",
            {"session_id": "session_1", "padding": "x" * 128},
            session_dir=tmp_path,
            created_at=100.0 + index,
        )

    run_root = tmp_path / "_run_journal" / "session_1"
    assert sorted(path.stem for path in run_root.glob("*.jsonl")) == [
        "run_3",
        "run_4",
        "run_5",
    ]


def test_closed_run_journal_retention_keeps_latest_under_byte_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUN_JOURNAL_MAX_CLOSED_RUNS", "20")
    monkeypatch.setenv("HERMES_WEBUI_RUN_JOURNAL_MAX_CLOSED_BYTES", "900")

    for index in range(4):
        append_run_event(
            "session_1",
            f"run_{index}",
            "stream_end",
            {"session_id": "session_1", "padding": "x" * 220},
            session_dir=tmp_path,
            created_at=200.0 + index,
        )

    run_root = tmp_path / "_run_journal" / "session_1"
    retained = sorted(path.stem for path in run_root.glob("*.jsonl"))
    assert retained
    assert retained[-1] == "run_3"
    assert sum(path.stat().st_size for path in run_root.glob("*.jsonl")) <= 900


def test_closed_run_journal_retention_never_removes_unfinished_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUN_JOURNAL_MAX_CLOSED_RUNS", "1")
    monkeypatch.setenv("HERMES_WEBUI_RUN_JOURNAL_MAX_CLOSED_BYTES", "1")

    append_run_event(
        "session_1",
        "run_live",
        "token",
        {"text": "still running"},
        session_dir=tmp_path,
        created_at=1.0,
    )
    append_run_event(
        "session_1",
        "run_closed",
        "stream_end",
        {"session_id": "session_1"},
        session_dir=tmp_path,
        created_at=2.0,
    )

    run_root = tmp_path / "_run_journal" / "session_1"
    assert (run_root / "run_live.jsonl").exists()
    assert (run_root / "run_closed.jsonl").exists()


def test_closed_run_journal_retention_adopts_legacy_terminal_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUN_JOURNAL_MAX_CLOSED_RUNS", "1")
    monkeypatch.setenv("HERMES_WEBUI_RUN_JOURNAL_MAX_CLOSED_BYTES", str(1024 * 1024))

    append_run_event(
        "session_1",
        "run_legacy",
        "stream_end",
        {"session_id": "session_1"},
        session_dir=tmp_path,
        created_at=1.0,
    )
    legacy_path = tmp_path / "_run_journal" / "session_1" / "run_legacy.jsonl"
    legacy_path.with_name("run_legacy.jsonl.closed").unlink()

    append_run_event(
        "session_1",
        "run_current",
        "stream_end",
        {"session_id": "session_1"},
        session_dir=tmp_path,
        created_at=2.0,
    )

    assert not legacy_path.exists()
    assert (legacy_path.parent / "run_current.jsonl").exists()


def test_closed_run_journal_retention_preserves_newest_close_when_older_closes_late(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_WEBUI_RUN_JOURNAL_MAX_CLOSED_RUNS", "1")
    monkeypatch.setenv("HERMES_WEBUI_RUN_JOURNAL_MAX_CLOSED_BYTES", str(1024 * 1024))

    append_run_event(
        "session_1",
        "run_newest",
        "stream_end",
        {"session_id": "session_1"},
        session_dir=tmp_path,
        created_at=20.0,
    )
    append_run_event(
        "session_1",
        "run_older",
        "stream_end",
        {"session_id": "session_1"},
        session_dir=tmp_path,
        created_at=10.0,
    )

    run_root = tmp_path / "_run_journal" / "session_1"
    assert not (run_root / "run_older.jsonl").exists()
    assert (run_root / "run_newest.jsonl").exists()


def test_nonterminal_append_reopens_closed_run_and_protects_it_from_retention(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_WEBUI_RUN_JOURNAL_MAX_CLOSED_RUNS", "1")
    monkeypatch.setenv("HERMES_WEBUI_RUN_JOURNAL_MAX_CLOSED_BYTES", str(1024 * 1024))

    append_run_event(
        "session_1",
        "run_reopened",
        "stream_end",
        {"session_id": "session_1"},
        session_dir=tmp_path,
        created_at=1.0,
    )
    append_run_event(
        "session_1",
        "run_reopened",
        "token",
        {"text": "late event"},
        session_dir=tmp_path,
        created_at=2.0,
    )
    append_run_event(
        "session_1",
        "run_current",
        "stream_end",
        {"session_id": "session_1"},
        session_dir=tmp_path,
        created_at=3.0,
    )

    run_root = tmp_path / "_run_journal" / "session_1"
    assert (run_root / "run_reopened.jsonl").exists()
    assert not (run_root / "run_reopened.jsonl.closed").exists()
    assert (run_root / "run_current.jsonl").exists()


def test_close_marker_failure_does_not_fail_durable_event_append(tmp_path, monkeypatch):
    def fail_marker(*_args, **_kwargs):
        raise OSError("forced marker failure")

    monkeypatch.setattr("api.run_journal._mark_run_closed", fail_marker)

    event = append_run_event(
        "session_1",
        "run_1",
        "stream_end",
        {"session_id": "session_1"},
        session_dir=tmp_path,
    )

    assert event["event"] == "stream_end"
    events = read_run_events("session_1", "run_1", session_dir=tmp_path)["events"]
    assert events[-1]["event"] == "stream_end"


def test_stale_close_marker_cannot_evict_run_after_reopen_unlink_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_WEBUI_RUN_JOURNAL_MAX_CLOSED_RUNS", "1")
    monkeypatch.setenv("HERMES_WEBUI_RUN_JOURNAL_MAX_CLOSED_BYTES", str(1024 * 1024))

    append_run_event(
        "session_1",
        "run_reopened",
        "stream_end",
        {"session_id": "session_1"},
        session_dir=tmp_path,
        created_at=1.0,
    )
    run_root = tmp_path / "_run_journal" / "session_1"
    stale_marker = run_root / "run_reopened.jsonl.closed"
    original_unlink = Path.unlink

    def fail_stale_marker_unlink(path, *args, **kwargs):
        if path == stale_marker:
            raise OSError("forced stale marker unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_stale_marker_unlink)
    append_run_event(
        "session_1",
        "run_reopened",
        "token",
        {"text": "late event"},
        session_dir=tmp_path,
        created_at=2.0,
    )
    append_run_event(
        "session_1",
        "run_current",
        "stream_end",
        {"session_id": "session_1"},
        session_dir=tmp_path,
        created_at=3.0,
    )

    assert (run_root / "run_reopened.jsonl").exists()
    events = read_run_events(
        "session_1", "run_reopened", session_dir=tmp_path
    )["events"]
    assert events[-1]["event"] == "token"


def test_writer_does_not_expose_reserved_sequence_before_reopen_append(
    tmp_path, monkeypatch
):
    writer = RunJournalWriter("session_1", "run_1", session_dir=tmp_path)
    observed = {}
    original_append = append_run_event

    def inspect_append(*args, **kwargs):
        observed["preassigned_seq"] = kwargs.get("seq")
        return original_append(*args, **kwargs)

    monkeypatch.setattr("api.run_journal.append_run_event", inspect_append)

    event = writer.append_sse_event("token", {"text": "atomic"})

    assert observed["preassigned_seq"] is None
    assert event["seq"] == 1


def test_prune_clears_sequence_cache_before_reopened_run_can_append(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_WEBUI_RUN_JOURNAL_MAX_CLOSED_RUNS", "1")
    monkeypatch.setenv("HERMES_WEBUI_RUN_JOURNAL_MAX_CLOSED_BYTES", str(1024 * 1024))
    append_run_event(
        "session_1",
        "victim",
        "stream_end",
        {},
        session_dir=tmp_path,
        created_at=1.0,
    )
    entered = threading.Event()
    release = threading.Event()
    original_evict = __import__(
        "api.run_journal", fromlist=["_evict_pruned_run_caches"]
    )._evict_pruned_run_caches

    def paused_evict(path):
        if path.name == "victim.jsonl":
            entered.set()
            assert release.wait(5)
        original_evict(path)

    monkeypatch.setattr("api.run_journal._evict_pruned_run_caches", paused_evict)
    errors = []

    def close_newer():
        try:
            append_run_event(
                "session_1",
                "newer",
                "stream_end",
                {},
                session_dir=tmp_path,
                created_at=2.0,
            )
        except BaseException as exc:
            errors.append(exc)

    prune_thread = threading.Thread(target=close_newer)
    prune_thread.start()
    assert entered.wait(5)

    reopened_result = {}

    def reopen_victim():
        reopened_result["event"] = append_run_event(
            "session_1",
            "victim",
            "token",
            {"text": "reopened"},
            session_dir=tmp_path,
            created_at=3.0,
        )

    reopen_thread = threading.Thread(target=reopen_victim)
    reopen_thread.start()
    release.set()
    prune_thread.join(5)
    reopen_thread.join(5)

    assert not errors
    assert not prune_thread.is_alive()
    assert not reopen_thread.is_alive()
    assert reopened_result["event"]["seq"] == 1
    following = append_run_event(
        "session_1",
        "victim",
        "token",
        {"text": "following"},
        session_dir=tmp_path,
        created_at=4.0,
    )
    assert following["seq"] == 2
    replay = read_session_run_events(
        "session_1", after_event_id="victim:1", session_dir=tmp_path
    )
    assert replay["status"] == "ok"
