from pathlib import Path
import json

from tests.frontend_contract_helpers import function_source, run_node

REPO_ROOT = Path(__file__).parent.parent
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def _body_from_brace(src: str, brace: int, label: str) -> str:
    assert brace >= 0, f"body opening brace not found for: {label}"
    depth = 1
    i = brace + 1
    while i < len(src) and depth:
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"body did not close for: {label}"
    return src[brace + 1 : i - 1]


def _function_body(name: str) -> str:
    marker = f"function {name}("
    start = MESSAGES_JS.find(marker)
    assert start >= 0, f"function not found: {name}"
    signature_end = MESSAGES_JS.find(")", start)
    assert signature_end >= 0, f"function signature not found: {name}"
    brace = MESSAGES_JS.find("{", signature_end)
    return _body_from_brace(MESSAGES_JS, brace, name)


def test_busy_dips_no_longer_force_stop_approval_polling():
    body = _function_body("_startApprovalFallbackPoll")

    assert "_approvalPollingSessionMissingOrMismatched(sid)" in body
    assert "!S.busy || !S.session || S.session.session_id !== sid" not in body


def test_empty_pending_clears_card_and_only_stops_after_confirmed_idle():
    fallback_body = _function_body("_startApprovalFallbackPoll")

    assert "else if (!_approvalPollingSessionMissingOrMismatched(sid)) {" in fallback_body
    assert "_clearApprovalPendingForSession(sid);" in fallback_body
    # An authoritative empty response must retire the stale card immediately.
    assert "_hideApprovalCardIfOwner(sid, true);" in fallback_body
    assert "if (!S.busy) {" in fallback_body
    assert "stopApprovalPollingForSession(sid);" in fallback_body
    assert "else if (!_approvalPollingSessionMissingOrMismatched(sid) && !S.busy)" not in fallback_body
    assert "stopApprovalPolling(); _hideApprovalCardIfOwner(sid, true); return;" in fallback_body


def approval_poll_outcome(*, busy, card_owner='owner', switch_during_fetch=False, pending=None):
    functions = '\n'.join(function_source(MESSAGES_JS, name) for name in (
        '_startApprovalFallbackPoll', '_hideApprovalCardIfOwner',
    ))
    return run_node(f"""
const S={{session:{{session_id:'owner'}},busy:{json.dumps(busy)}}};
const hidden=[],cleared=[],stopped=[],shown=[];
let _approvalSessionId={json.dumps(card_owner)}, _approvalFallbackPollInFlight=false, _approvalPollTimer;
const _approvalPendingBySession=new Map([['owner',{{pending:{{approval_id:'a'}}}}]]);
const _approvalPollingSessionMissingOrMismatched=sid=>S.session.session_id!==sid;
const _clearApprovalPendingForSession=sid=>{{cleared.push(sid);_approvalPendingBySession.delete(sid);}};
const _unmarkApprovalDismissed=()=>{{}};
const hideApprovalCard=force=>hidden.push(force);
const stopApprovalPollingForSession=sid=>stopped.push(sid);
const stopApprovalPolling=()=>stopped.push('all');
const showApprovalForSession=(sid,pending)=>shown.push([sid,pending]);
const setInterval=()=>1;
const api=async()=>{{
  if({json.dumps(switch_during_fetch)}) S.session.session_id='other';
  return {{pending:{json.dumps(pending)},pending_count:1}};
}};
{functions}
_startApprovalFallbackPoll('owner');
setImmediate(()=>console.log(JSON.stringify({{hidden,cleared,stopped,shown,inFlight:_approvalFallbackPollInFlight}})));
""")


def test_approval_poll_cleanup_is_owner_scoped_and_keeps_busy_polling():
    for busy in (True, False):
        result = approval_poll_outcome(busy=busy)
        assert result == {'hidden': [True], 'cleared': ['owner'],
                          'stopped': [] if busy else ['owner'], 'shown': [], 'inFlight': False}
    assert approval_poll_outcome(busy=True, card_owner='other')['hidden'] == []
    switched = approval_poll_outcome(busy=True, switch_during_fetch=True)
    assert switched['hidden'] == switched['cleared'] == switched['stopped'] == []
    pending = approval_poll_outcome(busy=True, pending={'approval_id': 'next'})
    assert pending['hidden'] == pending['cleared'] == []
    assert pending['shown'] == [['owner', {'approval_id': 'next'}]]
