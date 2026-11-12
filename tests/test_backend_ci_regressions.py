"""Behavioral guards for backend failures found by the full CI matrix."""
import json
import threading
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import Mock
from pathlib import Path

import pytest


@pytest.mark.parametrize("age,expected_stops", [(10, []), (121, ["expired-run"])])
def test_approval_pending_retained_run_expiry(monkeypatch, age, expected_stops):
    from api import route_approvals as approvals, routes
    from api import gateway_chat

    sid = "retained-pending-ci"
    entry = {
        "approval_id": "approval-1", "run_id": "expired-run", "command": "echo review",
        approvals._GATEWAY_MIRROR_FLAG: True,
        approvals._GATEWAY_MIRROR_RETAINED: True,
        "_gateway_mirror_created_at": 1000 - age,
    }
    pending = {sid: [entry]}
    # Both modules intentionally share the agent-owned approval state.
    for module in (approvals, routes):
        monkeypatch.setattr(module, "_pending", pending)
        monkeypatch.setattr(module, "_gateway_queues", {})
        monkeypatch.setattr(module, "_lock", threading.Lock())
    monkeypatch.setattr(approvals.time, "time", lambda: 1000)
    stopped = []
    monkeypatch.setattr(gateway_chat, "stop_gateway_run", stopped.append)
    monkeypatch.setattr(routes, "j", lambda handler, payload: payload)

    payload = routes._handle_approval_pending(SimpleNamespace(), urlparse(f"/api/approval/pending?session_id={sid}"))

    assert stopped == expected_stops
    assert payload["pending_count"] == (0 if expected_stops else 1)
    assert payload["pending"] == (None if expected_stops else entry)
    # Polling again must not cancel a run twice.
    routes._handle_approval_pending(SimpleNamespace(), urlparse(f"/api/approval/pending?session_id={sid}"))
    assert stopped == expected_stops


def test_persisted_count_does_not_load_or_rewrite_legacy_session(tmp_path, monkeypatch):
    from api import models

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    sid = "legacy-count-ci"
    partial = {"role": "assistant", "content": "progress", "_partial": True, "timestamp": 1}
    raw = json.dumps({"session_id": sid, "messages": [partial, partial, partial]}).encode()
    path = tmp_path / f"{sid}.json"
    path.write_bytes(raw)
    load = Mock(side_effect=AssertionError("a count read must not invoke self-healing Session.load"))
    monkeypatch.setattr(models.Session, "load", load)

    assert models._persisted_message_count(sid) == 3
    assert path.read_bytes() == raw
    assert not path.with_suffix(".json.bak").exists()
    load.assert_not_called()


@pytest.mark.parametrize("replace_every_read,expected", [(False, 4), (True, None)])
def test_legacy_count_rejects_replaced_snapshot(tmp_path, monkeypatch, replace_every_read, expected):
    from api import models

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    sid = "legacy-count-replacement-ci"
    path = tmp_path / f"{sid}.json"
    path.write_text(json.dumps({"messages": [{"content": "old"}] * 3}))
    read_text = Path.read_text
    reads = []

    def racing_read(target, *args, **kwargs):
        content = read_text(target, *args, **kwargs)
        if target == path:
            reads.append(content)
            if replace_every_read or len(reads) == 1:
                replacement = tmp_path / "replacement"
                replacement.write_text(json.dumps({"messages": [{"content": "new"}] * (3 + len(reads))}))
                replacement.replace(path)
        return content

    monkeypatch.setattr(Path, "read_text", racing_read)
    assert models._persisted_message_count(sid) == expected
    assert len(reads) == (3 if replace_every_read else 2)
    facts = models._legacy_sidecar_facts_get(sid)
    assert (facts is None) == replace_every_read
    if facts is not None:
        assert facts["message_count"] == expected
