"""Additional contracts for the completion notification integration."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_completion_notification_is_after_persisted_success_path():
    src = (ROOT / "api/streaming.py").read_text(encoding="utf-8")
    save = src.index("s.save()", src.index("if not ephemeral and s.messages:"))
    dispatch = src.index("dispatch_completed_turn", save)
    assert dispatch > save
    assert "if not ephemeral:" in src[save:dispatch]


def test_official_sender_success_and_concurrency_guards_are_required():
    src = (ROOT / "api/completion_notifications.py").read_text(encoding="utf-8")
    assert 'result.get("success") is not True' in src
    assert "send_message_tool" in src
    assert "_IN_FLIGHT" in src


def test_gateway_completion_path_dispatches_after_success_writeback():
    src = (ROOT / "api/gateway_chat.py").read_text(encoding="utf-8")
    committed = src.index("success_writeback_committed = True")
    dispatch = src.index("dispatch_completed_turn", committed)
    done = src.index('put_gateway_event("done"', dispatch)
    assert committed < dispatch < done


def test_settings_api_fallback_uses_canonical_platform_names():
    src = (ROOT / "api/routes.py").read_text(encoding="utf-8")
    assert '"weixin": False' in src
    assert '"wecom": False' in src
    assert '"wechat_work": False' not in src
