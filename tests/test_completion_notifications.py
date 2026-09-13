"""Completion notification contracts."""

import importlib
import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def notifications(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    (tmp_path / "hermes-home").mkdir()
    (tmp_path / "hermes-home" / ".env").write_text(
        "WEIXIN_TOKEN=secret\nWEIXIN_ACCOUNT_ID=account\nWEIXIN_HOME_CHANNEL=home\n"
        "WECOM_BOT_ID=bot\nWECOM_SECRET=secret\nWECOM_HOME_CHANNEL=home\n"
        "FEISHU_APP_ID=app\nFEISHU_APP_SECRET=secret\nFEISHU_HOME_CHANNEL=home\n",
        encoding="utf-8",
    )
    agent_root = tmp_path / "hermes-agent"
    (agent_root / "tools").mkdir(parents=True)
    (agent_root / "tools" / "send_message_tool.py").write_text("# fixture\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_AGENT_DIR", str(agent_root))
    monkeypatch.setenv("HERMES_WEBUI_AGENT_DIR", str(agent_root))
    module = importlib.import_module("api.completion_notifications")
    return importlib.reload(module)


def test_public_status_exposes_configuration_without_webhook_values(notifications):
    status = notifications.public_status()

    assert status["enabled"] is False
    assert status["channels"] == ["browser", "weixin", "wecom", "feishu"]
    assert status["configured"] == {"browser": True, "weixin": True, "wecom": True, "feishu": True}
    serialized = repr(status)
    assert "secret" not in serialized


def test_normalize_settings_rejects_unknown_or_unconfigured_channels(notifications):
    with pytest.raises(ValueError, match="Unsupported completion notification channel"):
        notifications.normalize_settings({
            "completion_notifications_enabled": True,
            "completion_notification_channels": ["sms"],
        })

    with pytest.raises(ValueError, match="not configured"):
        empty_home = Path(os.environ["HERMES_HOME"]).parent / "empty-home"
        empty_home.mkdir()
        notifications.normalize_settings({
            "completion_notifications_enabled": True,
            "completion_notification_channels": ["feishu"],
        }, environ={"HERMES_HOME": str(empty_home), "HERMES_AGENT_DIR": os.environ["HERMES_AGENT_DIR"]})


def test_send_completion_is_idempotent_and_uses_bounded_preview(notifications, monkeypatch):
    sent = []
    monkeypatch.setattr(notifications, "_send_via_hermes", lambda channel, message, home=None: sent.append((channel, message, home)) or {"success": True, "message_id": "m"})
    settings = {
        "completion_notifications_enabled": True,
        "completion_notification_channels": ["feishu"],
    }
    result1 = notifications.send_completion(
        settings,
        session_id="session-1",
        stream_id="stream-1",
        title="A session",
        text="x" * 900,
    )
    result2 = notifications.send_completion(
        settings,
        session_id="session-1",
        stream_id="stream-1",
        title="A session",
        text="x" * 900,
    )

    assert result1["sent"] == ["feishu"]
    assert result2["sent"] == []
    assert len(sent) == 1
    assert len(sent[0][1]) <= 500
    assert "x" * 20 not in sent[0][1]
    assert "A session" not in sent[0][1]


def test_interrupted_completion_is_not_sent(notifications, monkeypatch):
    monkeypatch.setattr(notifications, "_send_via_hermes", lambda *args: pytest.fail("interrupted turn was sent"))

    result = notifications.notify_turn_terminal(
        {"completion_notifications_enabled": True, "completion_notification_channels": ["feishu"]},
        session_id="session-1",
        stream_id="stream-1",
        status="interrupted",
        title="A session",
        text="partial",
    )

    assert result == {"sent": [], "skipped": "terminal_status"}


def test_official_sender_errors_are_failed_and_do_not_leak_details(notifications, monkeypatch):
    sleeps = []
    monkeypatch.setattr(notifications.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(
        notifications,
        "_run_sender_process",
        lambda *args, **kwargs: json.dumps({"error": "token=top-secret rate limited; cooldown active for 30.0s"}),
    )
    result = notifications.send_completion(
        {"completion_notifications_enabled": True, "completion_notification_channels": ["weixin"]},
        session_id="session-2",
        stream_id="stream-2",
        title="A session",
        text="done",
    )
    assert result == {"sent": [], "failed": ["weixin"]}
    assert "secret" not in repr(result)
    assert sleeps == [30.0, 30.0]


def test_sender_environment_is_scoped_and_state_is_private(notifications, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("WEIXIN_TOKEN", "selected-platform-token")
    env, _ = notifications._sender_environment("weixin")
    assert "OPENAI_API_KEY" not in env
    assert env["WEIXIN_TOKEN"] == "selected-platform-token"

    notifications._write_state({"session:stream": ["weixin"]})
    assert notifications._state_path().stat().st_mode & 0o777 == 0o600


def test_sender_subprocess_uses_official_contract_without_unrelated_secrets(notifications, monkeypatch):
    agent_root = Path(os.environ["HERMES_AGENT_DIR"])
    (agent_root / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (agent_root / "tools" / "send_message_tool.py").write_text(
        "import json, os\n"
        "def send_message_tool(args):\n"
        "    assert args['target'] == 'weixin'\n"
        "    assert 'OPENAI_API_KEY' not in os.environ\n"
        "    return json.dumps({'success': True, 'message_id': 'test-id'})\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    result = notifications._send_via_hermes("weixin", "test")
    assert result["success"] is True
    assert result["message_id"] == "test-id"


def test_profile_home_isolates_credentials_and_idempotency(notifications, monkeypatch, tmp_path):
    homes = [tmp_path / "profile-a", tmp_path / "profile-b"]
    for index, home in enumerate(homes):
        home.mkdir()
        (home / ".env").write_text(
            f"WEIXIN_TOKEN=token-{index}\nWEIXIN_ACCOUNT_ID=account-{index}\nWEIXIN_HOME_CHANNEL=home-{index}\n",
            encoding="utf-8",
        )
    seen = []
    monkeypatch.setattr(
        notifications,
        "_send_via_hermes",
        lambda channel, message, home=None: seen.append(str(home)) or {"success": True},
    )
    settings = {"completion_notifications_enabled": True, "completion_notification_channels": ["weixin"]}
    for home in homes:
        result = notifications.send_completion(
            settings,
            session_id="same-session",
            stream_id="same-stream",
            title="title",
            text="done",
            hermes_home=home,
        )
        assert result["sent"] == ["weixin"]
        assert notifications._state_path(home).is_file()
    assert seen == [str(home) for home in homes]


def test_public_status_is_scoped_to_requested_profile_home(notifications, tmp_path):
    configured_home = tmp_path / "configured-profile"
    unconfigured_home = tmp_path / "unconfigured-profile"
    configured_home.mkdir()
    unconfigured_home.mkdir()
    (configured_home / ".env").write_text(
        "WEIXIN_TOKEN=profile-token\nWEIXIN_ACCOUNT_ID=profile-account\nWEIXIN_HOME_CHANNEL=profile-home\n",
        encoding="utf-8",
    )
    assert notifications.public_status(configured_home)["configured"]["weixin"] is True
    assert notifications.public_status(unconfigured_home)["configured"]["weixin"] is False
