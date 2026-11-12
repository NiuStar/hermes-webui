"""Tests: server-side ``msg_limit`` ceiling on ``GET /api/session``.

A client could request ``msg_limit=1000000`` (or the frontend's
``msg_limit=9999`` outline-jump path) and force the server to assemble and
serialize an unbounded message payload. The handler now clamps ``msg_limit`` to
``_MAX_MSG_LIMIT`` (generous — far above any legitimate visible-row window) so
real pagination is unaffected while the pathological/oversized request is
bounded. The existing ``_messages_truncated`` signal covers the clamped case.

The parse+clamp lives in ``_parse_msg_limit`` so the clamping expression has
direct test coverage (driving the handler end-to-end would require a live
session + state.db; the helper is the unit under test).
"""
from __future__ import annotations

from api.routes import _MAX_MSG_LIMIT, _parse_msg_limit


def test_max_msg_limit_constant_is_reasonable():
    """The ceiling must exist and be well above legitimate pagination sizes
    (the frontend grows windows by ~30, initial load is 30) but finite."""
    assert isinstance(_MAX_MSG_LIMIT, int)
    assert 100 <= _MAX_MSG_LIMIT <= 2000, _MAX_MSG_LIMIT


def test_parse_msg_limit_none_when_absent_or_empty():
    """No value (the bare no-msg_limit path) returns None — intentionally the
    'full transcript' escape hatch for branch/undo/jump-to-start flows."""
    assert _parse_msg_limit(None) is None
    assert _parse_msg_limit("") is None


def test_parse_msg_limit_none_when_malformed():
    """A non-numeric value returns None rather than raising (matches the
    pre-fix behavior where a ValueError fell through to msg_limit=None)."""
    assert _parse_msg_limit("not-a-number") is None
    assert _parse_msg_limit("abc") is None


def test_parse_msg_limit_passes_legit_sizes_unchanged():
    """Legitimate pagination sizes (5/30/60/100) are returned verbatim — the
    ceiling only caps oversized requests."""
    for legit in (1, 5, 30, 60, 100):
        if legit > _MAX_MSG_LIMIT:
            continue
        assert _parse_msg_limit(str(legit)) == legit, f"legit {legit} altered"


def test_parse_msg_limit_clamps_oversized_to_ceiling():
    """An over-ceiling request (the outline-jump 9999, or a hostile 1000000) is
    clamped down to _MAX_MSG_LIMIT — exactly the regression this PR fixes."""
    assert _parse_msg_limit("9999") == _MAX_MSG_LIMIT
    assert _parse_msg_limit("1000000") == _MAX_MSG_LIMIT
    assert _parse_msg_limit(str(_MAX_MSG_LIMIT + 1)) == _MAX_MSG_LIMIT


def test_parse_msg_limit_ceiling_boundary_itself_passes():
    """A request exactly at the ceiling is allowed (clamp is inclusive)."""
    assert _parse_msg_limit(str(_MAX_MSG_LIMIT)) == _MAX_MSG_LIMIT


def test_parse_msg_limit_zero_and_negative_clamp_to_one():
    """Non-positive values clamp to the minimum (1), not None — a caller asking
    for msg_limit=0 gets a 1-row window, not the full transcript."""
    assert _parse_msg_limit("0") == 1
    assert _parse_msg_limit("-5") == 1


# ── #6177: metadata-decoupling — the frontend reads the ceiling from the
#    /api/session `_msg_limit_max` field instead of a hand-mirrored constant. ──

from pathlib import Path

_ROUTES_SRC = (Path(__file__).resolve().parents[1] / "api" / "routes.py").read_text(encoding="utf-8")
_SESSIONS_JS = (Path(__file__).resolve().parents[1] / "static" / "sessions.js").read_text(encoding="utf-8")


def test_backend_exposes_msg_limit_max_in_session_response():
    """The /api/session handler advertises the ceiling as `_msg_limit_max` so the
    frontend never has to hand-mirror _MAX_MSG_LIMIT (#6177 decoupling)."""
    assert 'raw["_msg_limit_max"] = _MAX_MSG_LIMIT' in _ROUTES_SRC


def test_frontend_declares_live_ceiling_at_module_scope_with_fallback():
    """`_msgLimitMax` MUST be declared at module scope (not an implicit global)
    with the static fallback, so the reload-width paths read a DEFINED value
    before the first /api/session response lands — otherwise a cold load reads
    `undefined`, drops msg_limit, and full-loads every session."""
    assert "let _msgLimitMax = _MSG_LIMIT_MAX;" in _SESSIONS_JS
    # refreshed from the response metadata, falling back when the server omits it
    assert "_msgLimitMax = data.session._msg_limit_max || _MSG_LIMIT_MAX;" in _SESSIONS_JS


def test_frontend_reload_width_paths_read_the_live_ceiling():
    """Both reload-width decisions read the live `_msgLimitMax`, not the mirror."""
    assert "Math.min(reloadLimit || _INITIAL_MSG_LIMIT, _msgLimitMax)" in _SESSIONS_JS
    assert "msg_limit=${boundedReloadLimit}" in _SESSIONS_JS
    assert "requestedLimit >= _msgLimitMax" in _SESSIONS_JS    # _loadOlderMessages


def test_frontend_reload_and_history_requests_obey_changed_live_ceiling():
    from tests.frontend_contract_helpers import function_source, run_node
    functions = '\n'.join(function_source(_SESSIONS_JS, name) for name in (
        '_ensureMessagesLoaded', '_loadOlderMessages', '_syncToolCallsForLoadedMessages',
    ))
    result = run_node(f"""
const window={{}}, requests=[];
let _loadingSessionId='s', _loadSessionGeneration=0, _messagesTruncated=true, _oldestIdx=100;
let _msgLimitMax=70, _loadingOlder=false, _messagesGeneration=0;
const _INITIAL_MSG_LIMIT=30, _MSG_LIMIT_MAX=500;
const S={{session:{{session_id:'s'}},messages:[],toolCalls:[]}};
const _messageReloadLimitForSession=()=>10000;
const _clearSameSessionForceReloadHint=()=>{{}};
const clearLiveToolCards=()=>{{}};
const _setSessionViewedCount=()=>{{}};
const api=async url=>{{
  requests.push(url);
  if(requests.length>1) return {{session:{{messages:[],_messages_truncated:false}}}};
  return {{session:{{messages:[{{role:'assistant',content:'loaded'}}],
    tool_calls:[{{name:'terminal',id:'new'}}], _msg_limit_max:40,
    _messages_truncated:true,_messages_offset:99}}}};
}};
{functions}
(async()=>{{
  await _ensureMessagesLoaded('s',{{force:true}});
  const tools=S.toolCalls;
  // A full window at the newly advertised ceiling must use msg_before.
  S.messages=Array.from({{length:40}},()=>({{role:'assistant',content:'loaded'}}));
  // No older rows: stop before rendering, but keep the real request decision.
  await _loadOlderMessages();
  console.log(JSON.stringify({{requests,tools,ceiling:_msgLimitMax,loading:_loadingOlder}}));
}})().catch(error=>{{console.error(error);process.exit(1);}});
""")
    from urllib.parse import parse_qs, urlsplit
    queries = [parse_qs(urlsplit(url).query) for url in result['requests']]
    assert len(queries) == 2
    assert queries[0]['msg_limit'] == ['70']
    assert queries[0]['messages'] == ['1']
    assert queries[1]['msg_limit'] == ['30']
    assert queries[1]['msg_before'] == ['99']
    assert result['tools'] == [{'name': 'terminal', 'id': 'new', 'done': True}]
    assert result['ceiling'] == 40
    assert result['loading'] is False
