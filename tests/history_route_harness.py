"""In-process route probes: synthetic sessions, no real state mutations."""
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import Mock
import ast
import threading
from pathlib import Path


def session_probe(monkeypatch, *, stream_id=None, live=False, messages=None, cli=False, query="messages=0"):
    import api.routes as r
    from api.models import Session
    s = Session(session_id="history-probe", title="History probe", workspace="/tmp",
                model="claude-opus-4-7", created_at=1, updated_at=1,
                messages=messages or [], tool_calls=[])
    s.active_stream_id = stream_id
    s.pending_user_message = "pending" if stream_id else None
    s.pending_attachments = ["pending.txt"] if stream_id else []
    s.pending_started_at = 2 if stream_id else None
    s.save = Mock()
    monkeypatch.setattr(r, "get_session", Mock(side_effect=KeyError("missing")) if cli else Mock(return_value=s))
    monkeypatch.setattr(r, "_session_visible_to_active_profile", lambda *_: True)
    # Exercise the real foreign-session synthesizer. It accepts no pagination
    # arguments: the fallback route currently returns the full CLI transcript.
    # Stub storage boundaries, not the synthesis or TODO/windowing algorithms.
    import api.models as models
    monkeypatch.setattr(r, "_session_index_marks_was_webui", lambda *_: False)
    monkeypatch.setattr(r, "_session_deleted_tombstone_marks_was_webui", lambda *_: False)
    missing_db = Mock(spec=Path)
    missing_db.exists.return_value = False
    monkeypatch.setattr(models, "_active_state_db_path", lambda: missing_db)
    monkeypatch.setattr(r, "get_cli_session_messages", Mock(return_value=s.messages))
    cli_meta = {"source_tag": "cli", "workspace": "/tmp", "model": s.model} if cli else {}
    monkeypatch.setattr(r, "_lookup_cli_session_metadata", lambda *_a, **_k: cli_meta)
    monkeypatch.setattr(r, "_metadata_only_message_summary", lambda *_a, **_k: {"message_count": len(s.messages), "last_message_at": 1})
    monkeypatch.setattr(r, "find_run_summary", lambda *_a, **_k: None)
    monkeypatch.setattr(r, "get_state_db_session_messages", lambda *_a, **_k: [])
    monkeypatch.setattr(r, "_webui_sidecar_lineage_messages_for_display", lambda *_a, **_k: s.messages)
    monkeypatch.setattr(r, "_merged_webui_lineage_messages_for_display", lambda _s, m: m)
    monkeypatch.setattr(r, "_pre_compression_continuation_session_id", lambda *_: None)
    monkeypatch.setattr(r, "STREAMS", {stream_id: object()} if live else {})
    monkeypatch.setattr(r, "_active_stream_ids", lambda: {stream_id} if live else set())
    monkeypatch.setattr(r, "_get_session_agent_lock", lambda *_: threading.RLock())
    monkeypatch.setattr(r, "j", lambda _h, data, **_kw: data)
    return s, lambda: r.handle_get(SimpleNamespace(), urlparse("/api/session?session_id=history-probe&" + query))


def delete_mutation_probe():
    """Execute the exact deletion mutation block with in-memory dependencies.

    Deliberately NOT full endpoint acceptance: no DELETE/POST request, DB calls,
    real Path.unlink, backup removal, or production-session access is possible.
    """
    import api.routes as routes
    tree = ast.parse(Path(routes.__file__).read_text())
    branch = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
                  and isinstance(n.test, ast.Compare)
                  and any(isinstance(c, ast.Constant) and c.value == "/api/session/delete" for c in n.test.comparators))
    start = next(i for i, n in enumerate(branch.body) if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "session_lock" for t in n.targets))
    end = next(i for i, n in enumerate(branch.body) if isinstance(n, ast.ImportFrom) and n.module == "api.config")
    fn = ast.FunctionDef(name="probe", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]), body=branch.body[start:end], decorator_list=[])
    path = Mock()
    path.__truediv__ = Mock(return_value=path)
    path.resolve.return_value = path
    path.exists.return_value = False
    sid = "synthetic-delete-only"
    index = {sid: {}, "keep": {"title": "unrelated"}}
    env = dict(sid=sid, SESSION_DIR=path, LOCK=threading.RLock(), SESSIONS={sid: object(), "keep": object()},
               _get_session_agent_lock=lambda _: threading.Lock(),
               prune_session_from_index=Mock(side_effect=lambda key: index.pop(key)),
               _record_webui_deleted_session_tombstone=Mock(), is_messaging_session=False,
               logger=Mock(), bad=Mock(side_effect=AssertionError("unexpected rejection")), handler=None)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), "<delete-mutation-probe>", "exec"), env)
    env["probe"]()
    # The shared mock must not hide a wrong-session path before unlink.
    path.__truediv__.assert_called_once_with(sid + ".json")
    return env, path, index
