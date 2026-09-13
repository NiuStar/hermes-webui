import hashlib
import json
import threading
import time

from api import models


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_session(session_dir, sid, *, active=False, rows=40):
    session = models.Session(
        session_id=sid,
        title="private title",
        messages=[
            {"role": "assistant", "content": ("payload-" + str(i)) * 40, "timestamp": float(i + 1)}
            for i in range(rows)
        ],
    )
    if active:
        session.active_stream_id = "private-stream"
        session.pending_user_message = "private prompt"
        session.pending_started_at = time.time()
    session.save(skip_index=True)
    models._message_offset_index_path(sid).unlink(missing_ok=True)
    return session.path


def test_maintenance_repairs_idle_large_sidecar_without_changing_body(tmp_path, monkeypatch):
    from api import session_sidecar_maintenance as maintenance

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    path = _write_session(session_dir, "idle_large")
    before = _sha(path)

    result = maintenance.run_sidecar_maintenance_once(
        session_dir=session_dir,
        threshold_bytes=1,
    )

    assert _sha(path) == before
    assert models._message_offset_index_path("idle_large").exists()
    assert result["candidate_sidecars"] == 1
    assert result["repaired_indexes"] == 1
    assert result["failed"] == 0
    rendered = json.dumps(result)
    assert "idle_large" not in rendered
    assert "private title" not in rendered
    assert "payload" not in rendered


def test_maintenance_skips_active_then_repairs_when_idle(tmp_path, monkeypatch):
    from api import session_sidecar_maintenance as maintenance

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    path = _write_session(session_dir, "active_large", active=True)

    first = maintenance.run_sidecar_maintenance_once(
        session_dir=session_dir,
        threshold_bytes=1,
    )
    assert first["skipped_active"] == 1
    assert not models._message_offset_index_path("active_large").exists()

    payload = json.loads(path.read_bytes())
    payload["active_stream_id"] = None
    payload["pending_user_message"] = None
    payload["pending_started_at"] = None
    path.write_text(json.dumps(payload), encoding="utf-8")
    second = maintenance.run_sidecar_maintenance_once(
        session_dir=session_dir,
        threshold_bytes=1,
    )
    assert second["repaired_indexes"] == 1
    assert models._message_offset_index_path("active_large").exists()


def test_maintenance_dry_run_never_writes_index(tmp_path, monkeypatch):
    from api import session_sidecar_maintenance as maintenance

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    path = _write_session(session_dir, "dry_run_large")
    before = _sha(path)

    result = maintenance.run_sidecar_maintenance_once(
        session_dir=session_dir,
        threshold_bytes=1,
        dry_run=True,
    )

    assert result["repairable_indexes"] == 1
    assert result["repaired_indexes"] == 0
    assert not models._message_offset_index_path("dry_run_large").exists()
    assert _sha(path) == before


def test_maintenance_skips_runtime_writeback_owner_even_when_metadata_is_idle(
    tmp_path, monkeypatch
):
    from api import config
    from api import session_sidecar_maintenance as maintenance

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    _write_session(session_dir, "runtime_busy")
    with config.SESSION_WRITEBACK_OWNERS_LOCK:
        config.SESSION_WRITEBACK_OWNERS["runtime_busy"] = "private-stream"
    try:
        result = maintenance.run_sidecar_maintenance_once(
            session_dir=session_dir,
            threshold_bytes=1,
        )
    finally:
        with config.SESSION_WRITEBACK_OWNERS_LOCK:
            config.SESSION_WRITEBACK_OWNERS.pop("runtime_busy", None)

    assert result["skipped_active"] == 1
    assert not models._message_offset_index_path("runtime_busy").exists()


def test_maintenance_worker_is_singleton(monkeypatch):
    from api import session_sidecar_maintenance as maintenance

    maintenance._reset_sidecar_maintenance_worker_for_tests()
    calls = []
    release = threading.Event()

    def hold_worker(**kwargs):
        calls.append(kwargs)
        release.wait(timeout=2)

    monkeypatch.setattr(
        maintenance,
        "_sidecar_maintenance_loop",
        hold_worker,
    )

    assert maintenance.start_sidecar_maintenance_worker(delay_seconds=0) is True
    deadline = time.time() + 2
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    assert calls
    assert maintenance.start_sidecar_maintenance_worker(delay_seconds=0) is False
    release.set()
    maintenance._reset_sidecar_maintenance_worker_for_tests()


def test_server_starts_sidecar_maintenance_worker_after_session_dir_exists():
    source = open("server.py", encoding="utf-8").read()
    mkdir_at = source.index("SESSION_DIR.mkdir(parents=True, exist_ok=True)")
    singleton_at = source.index("_abort_if_already_serving(HOST, PORT)")
    start_at = source.index("start_sidecar_maintenance_worker()")
    serve_at = source.index("httpd.serve_forever()")
    assert mkdir_at < singleton_at < start_at < serve_at


def test_automatic_maintenance_default_targets_high_risk_sidecars_only():
    from api import session_sidecar_maintenance as maintenance

    assert maintenance.DEFAULT_THRESHOLD_BYTES == 10 * 1024 * 1024
