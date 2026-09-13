"""Regression tests for bounded full-history paging and prefetch races.

``_loadOlderMessages`` still uses a generation token to prevent a late page
from mutating a transcript that another path replaced. ``_ensureAllMessagesLoaded``
no longer performs an unbounded wholesale GET; it repeatedly uses that bounded
pager and coalesces same-session callers through a shared promise.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    needle_async = f"async function {name}"
    needle_sync = f"function {name}"
    if needle_async in src:
        start = src.index(needle_async)
    else:
        start = src.index(needle_sync)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function {name!r} body not found")


def test_generation_token_declared_at_module_scope():
    assert "let _messagesGeneration = 0;" in SESSIONS_JS


def test_generation_bump_helper_exists():
    assert "function _bumpMessagesGeneration()" in SESSIONS_JS
    body = _function_body(SESSIONS_JS, "_bumpMessagesGeneration")
    assert "_messagesGeneration" in body


def test_load_older_snapshots_generation_before_await():
    body = _function_body(SESSIONS_JS, "_loadOlderMessages")
    snapshot_idx = body.index("const startGeneration = _messagesGeneration;")
    await_idx = body.index("await api(")
    assert snapshot_idx < await_idx


def test_load_older_aborts_when_generation_changed():
    body = _function_body(SESSIONS_JS, "_loadOlderMessages")
    assert "if (_messagesGeneration !== startGeneration) return;" in body


def test_load_older_generation_check_runs_before_replace():
    body = _function_body(SESSIONS_JS, "_loadOlderMessages")
    guard_idx = body.index("if (_messagesGeneration !== startGeneration) return;")
    replace_idx = body.index("S.messages = nextMessages;")
    assert guard_idx < replace_idx


def test_ensure_all_reuses_bounded_older_message_pager():
    body = _function_body(SESSIONS_JS, "_ensureAllMessagesLoaded")
    assert "await _loadOlderMessages();" in body
    assert "await api(" not in body
    assert "msg_limit=" not in body


def test_ensure_all_coalesces_same_session_calls():
    body = _function_body(SESSIONS_JS, "_ensureAllMessagesLoaded")
    assert "_ensureAllMessagesPromise && _ensureAllMessagesSid === sid" in body
    assert "return _ensureAllMessagesPromise;" in body
    assert "_ensureAllMessagesPromise = loadAll();" in body


def test_ensure_all_waits_for_prefetch_before_paging():
    body = _function_body(SESSIONS_JS, "_ensureAllMessagesLoaded")
    assert body.index("while (_loadingOlder)") < body.index("await _loadOlderMessages();")


def test_ensure_all_fails_closed_when_cursor_stalls():
    body = _function_body(SESSIONS_JS, "_ensureAllMessagesLoaded")
    assert "if (_oldestIdx >= previousOldest)" in body
    assert "Message pagination made no progress" in body


def test_ensure_all_guards_session_switch_after_each_await():
    body = _function_body(SESSIONS_JS, "_ensureAllMessagesLoaded")
    await_idx = body.index("await _loadOlderMessages();")
    sid_check_idx = body.index("S.session.session_id !== sid", await_idx)
    progress_idx = body.index("_oldestIdx >= previousOldest", await_idx)
    assert await_idx < sid_check_idx < progress_idx


def test_ensure_all_clears_shared_promise_in_finally():
    body = _function_body(SESSIONS_JS, "_ensureAllMessagesLoaded")
    finally_idx = body.index("} finally {")
    clear_idx = body.index("_ensureAllMessagesPromise = null;", finally_idx)
    assert clear_idx > finally_idx
