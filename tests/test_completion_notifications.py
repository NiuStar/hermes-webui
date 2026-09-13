"""Completion notification contracts."""

import importlib
import json
import os
import subprocess
import sys
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
    credential = "sk-" + "abcdefghijklmnop"
    monkeypatch.setattr(notifications, "_send_via_hermes", lambda channel, message, home=None: sent.append((channel, message, home)) or {"success": True, "message_id": "m"})
    settings = {
        "completion_notifications_enabled": True,
        "completion_notification_channels": ["feishu"],
    }
    result1 = notifications.send_completion(
        settings,
        session_id="session-1",
        stream_id="stream-1",
        title=f"生产验收\n会话 {credential}",
        text="部署成功，所有检查通过。 API_KEY=super-secret-value " + "x" * 900,
    )
    result2 = notifications.send_completion(
        settings,
        session_id="session-1",
        stream_id="stream-1",
        title=f"生产验收\n会话 {credential}",
        text="部署成功，所有检查通过。 API_KEY=super-secret-value " + "x" * 900,
    )

    assert result1["sent"] == ["feishu"]
    assert result2["sent"] == []
    assert len(sent) == 1
    message = sent[0][1]
    assert len(message) <= 500
    assert message.startswith("Hermes 回复完成\n会话：生产验收 会话 sk-abc...mnop\n输出结论：部署成功，所有检查通过。 API_KEY=***")
    assert "session-1" not in message
    assert "super-secret-value" not in message
    assert "abcdefghijklmnop" not in message
    assert "\n会话\n" not in message


def test_completion_text_has_safe_fallbacks_and_hard_total_limit(notifications):
    fallback = notifications._completion_text("\n\x00", "\t\n")
    assert fallback == "Hermes 回复完成\n会话：未命名会话\n输出结论：已完成，请返回 WebUI 查看结果。"

    safe_unicode = notifications._completion_text("生产\u202e标题", "完成\u200b✅")
    assert safe_unicode == "Hermes 回复完成\n会话：生产标题\n输出结论：完成✅"

    bounded = notifications._completion_text("标" * 500, "结论" * 5000)
    assert len(bounded) == 500
    assert bounded.splitlines()[1].startswith("会话：")
    assert bounded.splitlines()[1].endswith("…")
    assert bounded.splitlines()[2].startswith("输出结论：")


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


def test_sender_environment_is_scoped_and_state_is_private(notifications, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("WEIXIN_TOKEN", "selected-platform-token")
    env, _ = notifications._sender_environment("weixin")
    assert "OPENAI_API_KEY" not in env
    assert env["WEIXIN_TOKEN"] == "selected-platform-token"

    claimed = notifications._claimable_channels("session:stream", ["weixin"])
    assert claimed == ["weixin"]
    database = notifications._claims_db_path()
    assert database.stat().st_mode & 0o777 == 0o600


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
        assert notifications._claims_db_path(home).is_file()
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


def test_sender_cannot_be_shadowed_by_working_directory_tools_package(notifications, monkeypatch, tmp_path):
    agent_root = Path(os.environ["HERMES_AGENT_DIR"])
    (agent_root / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (agent_root / "tools" / "send_message_tool.py").write_text(
        "import json\ndef send_message_tool(args): return json.dumps({'success': True, 'origin': 'official'})\n",
        encoding="utf-8",
    )
    attacker = tmp_path / "attacker"
    (attacker / "tools").mkdir(parents=True)
    (attacker / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (attacker / "tools" / "send_message_tool.py").write_text(
        "raise RuntimeError('shadow package executed')\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(attacker)
    result = notifications._send_via_hermes("weixin", "test")
    assert result["origin"] == "official"


def test_claim_is_cross_process_at_most_once(notifications, tmp_path):
    home = tmp_path / "multiprocess-profile"
    home.mkdir()
    script = (
        "import json,sys\n"
        "from api.completion_notifications import _claimable_channels\n"
        "print(json.dumps(_claimable_channels('same-session:same-stream',['weixin'],sys.argv[1])))\n"
    )
    env = dict(os.environ)
    repo_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = repo_root
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(home)],
            cwd=repo_root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    completed = [worker.communicate(timeout=15) for worker in workers]
    for worker, (_stdout, stderr) in zip(workers, completed, strict=True):
        assert worker.returncode == 0, stderr
    results = [json.loads(stdout) for stdout, _stderr in completed]
    assert sorted(results, key=len) == [[], ["weixin"]]


def test_invalid_agent_override_falls_back_to_authoritative_agent_dir(notifications, monkeypatch, tmp_path):
    authoritative = tmp_path / "authoritative-agent"
    (authoritative / "tools").mkdir(parents=True)
    (authoritative / "tools" / "send_message_tool.py").write_text("# official\n", encoding="utf-8")
    invalid = tmp_path / "missing-agent"
    monkeypatch.setattr(notifications, "_AGENT_DIR", authoritative)
    root = notifications._agent_root({
        "HERMES_HOME": os.environ["HERMES_HOME"],
        "HERMES_WEBUI_AGENT_DIR": str(invalid),
    })
    assert root == authoritative


def test_failed_delivery_keeps_claim_and_prevents_duplicate_callback(notifications, monkeypatch):
    attempts = []
    monkeypatch.setattr(
        notifications,
        "_send_via_hermes",
        lambda *args: attempts.append(args) or (_ for _ in ()).throw(RuntimeError("failed")),
    )
    settings = {"completion_notifications_enabled": True, "completion_notification_channels": ["weixin"]}
    first = notifications.send_completion(
        settings,
        session_id="failed-session",
        stream_id="failed-stream",
        title="ignored",
        text="ignored",
    )
    second = notifications.send_completion(
        settings,
        session_id="failed-session",
        stream_id="failed-stream",
        title="ignored",
        text="ignored",
    )
    assert first == {"sent": [], "failed": ["weixin"]}
    assert second == {"sent": [], "failed": []}
    assert len(attempts) == 1


def test_legacy_r2_sent_state_prevents_duplicate_after_upgrade(notifications):
    notifications._write_state({"legacy-session:legacy-stream": ["weixin"]})
    assert notifications._claimable_channels(
        "legacy-session:legacy-stream",
        ["weixin"],
    ) == []


def test_claim_survives_process_exit_before_delivery(notifications, tmp_path):
    home = tmp_path / "crash-profile"
    home.mkdir()
    repo_root = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = repo_root
    script = (
        "from api.completion_notifications import _claimable_channels\n"
        "assert _claimable_channels('crash-session:crash-stream',['weixin'],%r)==['weixin']\n"
    ) % str(home)
    subprocess.run([sys.executable, "-c", script], cwd=repo_root, env=env, check=True)
    assert notifications._claimable_channels(
        "crash-session:crash-stream",
        ["weixin"],
        home,
    ) == []


def test_claims_use_one_private_transactional_database(notifications, tmp_path):
    home = tmp_path / "database-profile"
    home.mkdir()
    for index in range(20):
        assert notifications._claimable_channels(
            f"session-{index}:stream-{index}",
            ["weixin"],
            home,
        ) == ["weixin"]
    database = notifications._claims_db_path(home)
    assert database.is_file()
    assert database.stat().st_mode & 0o777 == 0o600
    assert not list(database.parent.glob("completion_notifications.claims/*"))


def test_uncertain_timeout_is_not_retried(notifications, monkeypatch):
    calls = []
    monkeypatch.setattr(
        notifications,
        "_send_via_hermes",
        lambda *args: calls.append(args) or (_ for _ in ()).throw(subprocess.TimeoutExpired("sender", 45)),
    )
    settings = {"completion_notifications_enabled": True, "completion_notification_channels": ["weixin"]}
    first = notifications.send_completion(
        settings,
        session_id="timeout-session",
        stream_id="timeout-stream",
        title="ignored",
        text="ignored",
    )
    second = notifications.send_completion(
        settings,
        session_id="timeout-session",
        stream_id="timeout-stream",
        title="ignored",
        text="ignored",
    )
    assert first == {"sent": [], "failed": ["weixin"]}
    assert second == {"sent": [], "failed": []}
    assert len(calls) == 1
