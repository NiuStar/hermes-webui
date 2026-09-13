from pathlib import Path

from api import models, routes


def test_sidecar_storage_health_is_aggregate_only_and_stat_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "SESSION_DIR", tmp_path)
    sizes = {
        "small.json": 10,
        "ten.json": 10 * 1024 * 1024,
        "twentyfive.json": 25 * 1024 * 1024,
        "fifty.json": 50 * 1024 * 1024,
        "_index.json": 99 * 1024 * 1024,
    }
    for name, size in sizes.items():
        path = tmp_path / name
        with path.open("wb") as handle:
            handle.truncate(size)
    index_dir = tmp_path / ".message_offsets"
    index_dir.mkdir()
    (index_dir / "one.json").write_text("{}")
    (index_dir / "two.json").write_text("{}")

    payload = routes._sidecar_storage_health()

    assert payload == {
        "status": "ok",
        "sidecar_count": 4,
        "indexed_sidecar_count": 2,
        "over_10mb": 3,
        "over_25mb": 2,
        "over_50mb": 1,
        "max_sidecar_bytes": 50 * 1024 * 1024,
        "full_session_resolve": {
            "max_concurrent": models._FULL_SESSION_RESOLVE_MAX_CONCURRENT,
            "inflight_sessions": 0,
        },
        "maintenance": {
            "worker_running": False,
            "last_run": None,
        },
    }
    rendered = repr(payload)
    for name in sizes:
        assert name not in rendered


def test_sidecar_storage_health_fails_closed_without_paths(tmp_path, monkeypatch):
    missing = tmp_path / "missing"
    monkeypatch.setattr(routes, "SESSION_DIR", missing)
    monkeypatch.setattr(Path, "glob", lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("secret/path")))

    payload = routes._sidecar_storage_health()

    assert payload == {"status": "error", "error": "PermissionError"}
    assert "secret" not in repr(payload)
