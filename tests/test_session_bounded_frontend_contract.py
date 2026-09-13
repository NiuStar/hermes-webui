from pathlib import Path
import json
import re
import shutil
import subprocess

import pytest


STATIC = Path(__file__).resolve().parents[1] / "static"


def test_production_javascript_never_requests_unbounded_session_messages():
    violations = []
    for path in STATIC.glob("*.js"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"/api/session\?session_id=[^\n'\"`]*", text):
            request = match.group(0)
            if "messages=1" in request and "msg_limit=" not in request:
                violations.append(f"{path.name}: {request}")
    assert not violations, "unbounded session message requests:\n" + "\n".join(violations)


def test_ensure_all_messages_uses_bounded_pager():
    text = (STATIC / "sessions.js").read_text(encoding="utf-8")
    start = text.index("async function _ensureAllMessagesLoaded()")
    end = text.index("\nconst SESSION_ARCHIVED_PAGE_SIZE", start)
    body = text[start:end]
    assert "await _loadOlderMessages()" in body
    assert "msg_limit=" not in body
    assert "Message pagination made no progress" in body
    assert "_ensureAllMessagesPromise" in body


def test_outline_reuses_bounded_full_history_loader():
    text = (STATIC / "outline.js").read_text(encoding="utf-8")
    assert "_ensureAllMessagesLoaded()" in text
    assert "/api/session?session_id=" not in text


def test_recovery_paths_adopt_pagination_cursor():
    text = (STATIC / "messages.js").read_text(encoding="utf-8")
    assert text.count("_messagesTruncated=!!") >= 2
    assert text.count("_oldestIdx=Number(") >= 2


def test_terminal_paths_merge_segmented_snapshots_without_resetting_cursor():
    text = (STATIC / "messages.js").read_text(encoding="utf-8")
    assert "function _mergeSegmentedTerminalMessages" in text
    assert "d.session&&d.session._messages_segmented" in text
    assert "d.session._messages_segmented" in text
    assert "sessionPayload._messages_segmented||sessionPayload._messages_truncated" in text
    assert "session._messages_segmented||session._messages_truncated" in text
    assert "if(!_doneSegmented&&typeof _oldestIdx" in text
    assert "if(!_cancelMergedWindow&&typeof _oldestIdx" in text
    assert "if(!_settledMergedWindow&&typeof _oldestIdx" in text


def _js_function(source: str, name: str) -> str:
    start = source.index(f"function {name}")
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unterminated function {name}")


def test_segmented_terminal_merge_finds_window_inside_physical_segment():
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    text = (STATIC / "messages.js").read_text(encoding="utf-8")
    functions = "\n".join(
        _js_function(text, name)
        for name in ("_messageIdentityKey", "_mergeSegmentedTerminalMessages")
    )
    script = f"""
{functions}
const m=(content,timestamp)=>({{role:'assistant',content,timestamp}});
const segment=[m('marker',1),m('a',2),m('repeat',3),m('repeat',3),m('new',4)];
const middle=_mergeSegmentedTerminalMessages([m('a',2),m('repeat',3)],segment);
const tie=_mergeSegmentedTerminalMessages([m('repeat',3)],segment);
const full=_mergeSegmentedTerminalMessages([m('old',0),...segment],segment);
const none=_mergeSegmentedTerminalMessages([m('old',0)],[m('new',4)]);
process.stdout.write(JSON.stringify({{middle,tie,full,none}}));
"""
    result = subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    assert [row["content"] for row in payload["middle"]] == ["a", "repeat", "repeat", "new"]
    assert [row["content"] for row in payload["tie"]] == ["repeat", "new"]
    assert [row["content"] for row in payload["full"]] == ["old", "marker", "a", "repeat", "repeat", "new"]
    assert [row["content"] for row in payload["none"]] == ["old", "new"]
