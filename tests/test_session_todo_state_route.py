"""TODO state must survive both WebUI sidecars and CLI fallback projection."""
import json
import pytest
import api.routes as routes
from tests.history_route_harness import session_probe


def test_routes_imports_attach_todo_state():
    from api.todo_state import attach_todo_state
    assert routes.attach_todo_state is attach_todo_state


@pytest.mark.parametrize("cli", [False, True], ids=["webui", "cli-fallback"])
def test_routes_attach_todo_state_from_webui_and_cli_session_paths(monkeypatch, cli):
    messages = [{"role": "tool", "name": "todo", "content":
                 '{"todos":[{"id":"1","content":"First","status":"completed"},'
                 '{"id":"2","content":"Second","status":"pending"}]}'}]
    _, request = session_probe(monkeypatch, messages=messages, cli=cli,
                               query="messages=1&resolve_model=0")
    state = request()["session"]["todo_state"]
    assert [(t["id"], t["content"], t["status"]) for t in state["todos"]] == [
        ("1", "First", "completed"), ("2", "Second", "pending")]


@pytest.mark.parametrize("cli", [False, True], ids=["webui", "cli-fallback"])
@pytest.mark.parametrize("latest", [[], [{"id": "new", "content": "Latest", "status": "pending"}]])
def test_latest_todo_survives_webui_window_and_cli_full_fallback(monkeypatch, cli, latest):
    messages = [
        {"role": "tool", "name": "todo", "content": json.dumps({"todos": [{"id": "old", "content": "Old", "status": "pending"}]})},
        {"role": "tool", "name": "todo", "content": json.dumps({"todos": latest})},
        {"role": "assistant", "content": "Final visible row"},
    ]
    _, request = session_probe(monkeypatch, messages=messages, cli=cli,
                               query="messages=1&msg_limit=1&resolve_model=0")
    response = request()["session"]
    if cli:
        # Legacy CLI fallback does not apply msg_limit. Do not manufacture a
        # paged transcript in the fixture and accidentally test the mock.
        assert response["messages"] == messages
        assert response["is_cli_session"] is True
        routes.get_cli_session_messages.assert_called_once_with("history-probe")
    else:
        assert response["messages"] == [messages[-1]]
        assert response["_messages_offset"] == 2
        assert response["_messages_truncated"] is True
    assert response["message_count"] == 3
    assert response["todo_state"]["todos"] == latest
