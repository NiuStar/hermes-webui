"""Contracts for automatic capacity-based conversation continuation."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTES = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
CONFIG = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
MESSAGES = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def test_default_capacity_guard_is_conservative_and_configurable():
    assert '"session_continuation_max_messages": 8000' in CONFIG
    assert '"session_continuation_max_bytes": 50 * 1024 * 1024' in CONFIG
    assert '"session_continuation_max_messages": (100, 1000000)' in CONFIG
    assert '"session_continuation_max_bytes": (1048576, 1073741824)' in CONFIG


def test_capacity_probe_does_not_load_full_transcript():
    start = ROUTES.index("def _capacity_status_from_disk(")
    end = ROUTES.index("def _sidecar_lineage_exceeds_threshold(", start)
    body = ROUTES[start:end]
    assert "stat()" in body
    assert "_read_metadata_json_prefix" in body
    assert "Session.load(" not in body
    assert "read_text(" not in body


def test_capacity_continuation_preserves_parent_and_starts_empty_child():
    start = ROUTES.index("def _create_capacity_continuation(")
    end = ROUTES.index("def _capacity_status_from_disk(", start)
    body = ROUTES[start:end]
    assert "messages=[]" in body
    assert "parent_session_id" in body
    assert 'session_source="fork"' in body
    assert "copy.deepcopy(getattr(source, \"enabled_toolsets\"" in body
    assert "source.messages" not in body
    assert "source.save" not in body


def test_send_checks_capacity_before_upload_and_optimistic_render():
    guard = MESSAGES.index("// Capacity guard:")
    upload = MESSAGES.index("uploadPendingFiles", guard)
    optimistic = MESSAGES.index("const userMsg=", guard)
    assert "/api/session/capacity-continuation" in MESSAGES[guard:upload]
    assert guard < upload < optimistic
    assert "continuation_prompt_prefix" in MESSAGES[guard:upload]
    assert "S.messages=[]" in MESSAGES[guard:upload]
