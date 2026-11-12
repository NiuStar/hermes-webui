from pathlib import Path
from tests.frontend_contract_helpers import function_source, run_node


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def test_sessions_js_resyncs_tool_calls_after_history_window_replacement():
    """History paging replaces S.messages with a larger window.

    Legacy sessions keep tool card data in session.tool_calls, so that side data
    must be refreshed alongside the message window. Otherwise renderMessages()
    can keep stale anchors and show unloaded/thinking placeholders while the
    user scrolls through history.
    """
    assert "function _syncToolCallsForLoadedMessages(messages, sessionToolCalls)" in SESSIONS_JS
    assert "_syncToolCallsForLoadedMessages(msgs, data.session.tool_calls);" in SESSIONS_JS
    assert "S.messages = nextMessages;\n    _syncToolCallsForLoadedMessages(nextMessages, responseSession.tool_calls);" in SESSIONS_JS
    # Full-history loading now reuses bounded paging instead of a third wholesale
    # replacement path; its tool-call synchronization is owned by that pager.
    from tests.frontend_contract_helpers import function_source
    full_history = function_source(SESSIONS_JS, '_ensureAllMessagesLoaded')
    assert "await _loadOlderMessages()" in full_history
    assert "S.messages =" not in full_history


def test_sessions_js_clears_session_tool_calls_when_messages_have_own_metadata():
    assert "const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;" in SESSIONS_JS
    assert "const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');" in SESSIONS_JS
    assert "windowOffset" not in SESSIONS_JS
    assert "copy.assistant_msg_idx=idx-offset;" not in SESSIONS_JS
    assert "S.toolCalls=sessionToolCalls.map(tc=>({...tc,done:true}));" in SESSIONS_JS
    assert "S.toolCalls=[];" in SESSIONS_JS


def test_full_history_loader_synchronizes_tools_before_rendering_each_window():
    functions = '\n'.join(function_source(SESSIONS_JS, name) for name in (
        '_loadOlderMessages', '_ensureAllMessagesLoaded', '_syncToolCallsForLoadedMessages',
    ))
    result = run_node(f"""
const window={{}}, snapshots=[], requests=[];
const _INITIAL_MSG_LIMIT=2, MESSAGE_RENDER_WINDOW_DEFAULT=50;
let _loadingOlder=false,_messagesTruncated=true,_oldestIdx=2,_messagesGeneration=0;
let _loadingSessionId=null,_msgLimitMax=500,_ensureAllMessagesPromise=null,_ensureAllMessagesSid=null;
let _messageRenderWindowSize=50,_scrollPinned=true;
const older={{role:'assistant',content:'older'}},current={{role:'assistant',content:'current'}};
const S={{session:{{session_id:'s'}},messages:[current],toolCalls:[{{id:'stale'}}]}};
const $=()=>null, msgContent=m=>m.content;
const _currentMessageRenderWindowSize=()=>50;
const _sameTranscriptMessage=(a,b)=>a.content===b.content;
const renderMessages=()=>snapshots.push({{messages:S.messages.map(m=>m.content),tools:S.toolCalls.map(t=>t.id)}});
const api=async url=>{{
  requests.push(url);
  return {{session:{{messages:url.includes('msg_before=')?[older]:[older,current],
    tool_calls:[{{id:'fresh'}}],_messages_truncated:false,_messages_offset:0}}}};
}};
{functions}
(async()=>{{
  await _ensureAllMessagesLoaded();
  S.messages=[current];S.toolCalls=[{{id:'stale'}}];_messagesTruncated=true;_oldestIdx=2;_msgLimitMax=3;
  await _ensureAllMessagesLoaded();
  console.log(JSON.stringify({{requests,snapshots,loading:_loadingOlder,owner:_ensureAllMessagesSid,promise:_ensureAllMessagesPromise}}));
}})().catch(error=>{{console.error(error);process.exit(1);}});
""")
    assert result['snapshots'] == [{'messages': ['older', 'current'], 'tools': ['fresh']}] * 2
    assert len(result['requests']) == 2
    assert 'msg_before=' not in result['requests'][0]
    assert 'msg_before=2&msg_limit=2' in result['requests'][1]
    assert result['loading'] is False
    assert result['owner'] is result['promise'] is None
