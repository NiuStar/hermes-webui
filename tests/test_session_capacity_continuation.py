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


def test_capacity_prompt_contains_authoritative_project_and_parent_identity():
    start = ROUTES.index("def _capacity_continuation_prompt(")
    end = ROUTES.index("def _sidecar_lineage_exceeds_threshold(", start)
    body = ROUTES[start:end]
    assert "Original project title" in body
    assert "Original project ID" in body
    assert "Original session title" in body
    assert "Original session ID" in body
    assert "load_projects()" in body
    assert "project_id" in body
    assert "profile" in body


def test_both_capacity_paths_use_parent_metadata_prompt():
    endpoint = ROUTES[ROUTES.index('parsed.path == "/api/session/capacity-continuation"'):]
    chat_start = ROUTES[ROUTES.index("def _handle_chat_start("):]
    assert "_capacity_continuation_prompt(source)" in endpoint
    assert "_capacity_continuation_prompt(_capacity_source)" in chat_start


def test_send_checks_capacity_before_upload_and_optimistic_render():
    guard = MESSAGES.index("// Capacity guard:")
    upload = MESSAGES.index("uploadPendingFiles", guard)
    optimistic = MESSAGES.index("const userMsg=", guard)
    assert "/api/session/capacity-continuation" in MESSAGES[guard:upload]
    assert guard < upload < optimistic
    guard_body = MESSAGES[guard:upload]
    assert "continuation_prompt_prefix" in guard_body
    assert "await loadSession(_childSid)" in guard_body
    assert "S.session&&S.session.session_id===_childSid" in guard_body
    assert "_sendInProgressSid=activeSid" in guard_body
    assert "_setActiveSessionUrl" not in guard_body
    assert "S.messages=[]" not in guard_body
