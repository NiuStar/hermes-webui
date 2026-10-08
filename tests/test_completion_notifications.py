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
    module = importlib.reload(module)
    monkeypatch.setattr(module, "_sender_dependency_ready", lambda channel: True, raising=False)
    return module


def test_public_status_exposes_configuration_without_webhook_values(notifications):
    status = notifications.public_status()

    assert status["enabled"] is False
    assert status["channels"] == ["browser", "weixin", "wecom", "feishu"]
    assert status["configured"] == {"browser": True, "weixin": True, "wecom": True, "feishu": True}
    serialized = repr(status)
    assert "secret" not in serialized


def test_feishu_is_not_selectable_without_sender_sdk(notifications, monkeypatch):
    monkeypatch.setattr(notifications, "_sender_dependency_ready", lambda channel: channel != "feishu")
    assert notifications.public_status()["configured"]["feishu"] is False
    with pytest.raises(ValueError, match="not configured"):
        notifications.normalize_settings({
            "completion_notifications_enabled": True,
            "completion_notification_channels": ["feishu"],
        })


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
    monkeypatch.setattr(notifications, "_run_feishu_card_process", lambda card, home: sent.append(("feishu", card, home)) or {"success": True, "message_id": "m"})
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
    card = sent[0][1]
    message = card["elements"][0]["content"]
    assert len(message) < 550
    assert "生产验收 会话 sk&#45;abc&#46;&#46;&#46;mnop" in message
    assert "部署成功，所有检查通过。 API&#95;KEY=&#42;&#42;&#42;" in message
    assert "session-1" not in repr(card)
    assert "super-secret-value" not in repr(card)
    assert "abcdefghijklmnop" not in repr(card)


def test_completion_counts_are_for_this_session_only(notifications, monkeypatch):
    from api import routes
    from types import SimpleNamespace

    home = Path(os.environ["HERMES_HOME"])
    monkeypatch.setattr(routes, "get_session", lambda sid, **kw: SimpleNamespace(profile="default"))
    monkeypatch.setattr(routes, "_active_auxiliary_task_inventory", lambda profile, sid: [
        {"type": "delegation", "parent_session_id": "session-1", "count": 2},
        {"type": "process", "parent_session_id": "session-1", "count": 1},
        {"type": "delegation", "parent_session_id": "another-session", "count": 9},
    ])
    monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda name: home)
    sent = []
    monkeypatch.setattr(notifications, "_run_feishu_card_process", lambda card, profile_home: sent.append(card) or {"success": True, "message_id": "m"})
    result = notifications.send_completion(
        {"completion_notifications_enabled": True, "completion_notification_channels": ["feishu"]},
        session_id="session-1", stream_id="stream-1", title="主题", text="完成", hermes_home=home,
    )
    assert result["sent"] == ["feishu"]
    assert "子代理 2 · 后台进程 1" in sent[0]["elements"][0]["content"]
    assert "9" not in repr(sent[0])


def test_completion_inventory_failure_never_reports_false_zero(notifications, monkeypatch):
    from api import routes

    monkeypatch.setattr(routes, "get_session", lambda sid, **kw: (_ for _ in ()).throw(RuntimeError("inventory unavailable")))
    sent = []
    monkeypatch.setattr(notifications, "_run_feishu_card_process", lambda card, home: sent.append(card) or {"success": True, "message_id": "m"})
    notifications.send_completion(
        {"completion_notifications_enabled": True, "completion_notification_channels": ["feishu"]},
        session_id="session-1", stream_id="stream-2", title="主题", text="完成",
    )
    assert "子代理 未知 · 后台进程 未知" in sent[0]["elements"][0]["content"]


def test_completion_counts_reject_mismatched_profile_home(notifications, monkeypatch, tmp_path):
    from api import routes
    from types import SimpleNamespace

    foreign = tmp_path / "other-profile"
    foreign.mkdir()
    monkeypatch.setattr(routes, "get_session", lambda sid, **kw: SimpleNamespace(profile="other"))
    monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda name: foreign)
    monkeypatch.setattr(routes, "_active_auxiliary_task_inventory", lambda profile, sid: pytest.fail("cross-profile inventory read"))
    assert notifications._session_auxiliary_counts("session-1", Path(os.environ["HERMES_HOME"])) is None


def test_completion_preview_reserves_space_for_counts(notifications):
    message = notifications._completion_text("标题", "结论" * 5000, (3, 4))
    assert len(message) <= 500
    assert "当前会话正在进行中子代理数量：3" in message
    assert "后台进程数量：4" in message


def test_feishu_card_has_hierarchy_and_safe_session_link(notifications, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PUBLIC_URL", "https://10.126.126.10:8787/")
    card = notifications._feishu_completion_card("A **title**", "结论\n第二行", (2, 3), "session-1")
    assert card["header"]["title"]["content"] == "Hermes · 回复完成"
    assert card["header"]["template"] == "blue"
    assert "text_size" not in card["elements"][0]
    body = card["elements"][0]["content"]
    assert "A &#42;&#42;title&#42;&#42;" in body and "子代理 2 · 后台进程 3" in body
    assert "通知生成时" in body
    assert card["elements"][1]["actions"][0]["url"] == "https://10.126.126.10:8787/session/session-1"


def test_feishu_card_renders_bold_in_redacted_conclusion_without_active_links(notifications):
    summary = "这是一批**较早任务的延迟通知**；[查看](https://invalid.example) <at id=all></at> API_KEY=secret-value"
    card = notifications._feishu_completion_card("标题", summary, (0, 0), "session-1")
    body = card["elements"][0]["content"]
    assert "**较早任务的延迟通知**" in body
    assert "\\*\\*较早任务的延迟通知\\*\\*" not in body
    assert "[查看](https://invalid.example)" not in body
    assert "<at id=all>" not in body
    assert "secret-value" not in repr(card)


def test_feishu_rejected_card_fallback_formats_summary_bold(notifications, monkeypatch):
    calls = []
    monkeypatch.setattr(notifications, "_run_feishu_card_process", lambda card, home: {"card_rejected": True})
    monkeypatch.setattr(notifications, "_send_via_hermes", lambda channel, text, home: calls.append((channel, text)) or {"success": True})
    notifications._send_feishu_completion("标题", "这是**较早任务的延迟通知**，不要 <at id=all></at>", (0, 0), "session-1", None)
    assert calls[0][0] == "feishu"
    assert "<b>较早任务的延迟通知</b>" in calls[0][1]
    assert "**较早任务的延迟通知**" not in calls[0][1]
    assert "<at id=all>" not in calls[0][1]


def test_feishu_markup_neutralizes_tags_and_links_but_keeps_only_bold(notifications):
    rendered = notifications._feishu_safe_markup("**加粗** [点击](https://x.example) <at id=all></at> & <b>伪标签</b>", card=True)
    assert "**加粗**" in rendered
    assert "[点击](https://x.example)" not in rendered
    assert "<at" not in rendered and "<b>" not in rendered
    assert "&#38;" in rendered
    fallback = notifications._feishu_safe_markup("**加粗** [点击](https://x.example) <at id=all></at> & <b>伪标签</b>", card=False)
    assert "<b>加粗</b>" in fallback
    assert "[点击](https://x.example)" not in fallback
    assert "<at" not in fallback and "&lt;at" in fallback
    assert "&amp;" in fallback


def test_feishu_card_rejects_untrusted_link_and_redacts_content(notifications, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PUBLIC_URL", "https://attacker.example/?token=secret")
    card = notifications._feishu_completion_card("A", "API_KEY=secret-value", None, "session-1")
    assert len(card["elements"]) == 1
    assert "secret-value" not in repr(card)
    assert "未知" in card["elements"][0]["content"]


def test_feishu_card_invalid_port_does_not_block_delivery(notifications, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PUBLIC_URL", "http://10.126.126.10:bad")
    card = notifications._feishu_completion_card("test", "done", (0, 0), "session-1")
    assert len(card["elements"]) == 1


def test_feishu_card_supports_explicit_private_webui_origin(notifications, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PUBLIC_URL", "http://10.126.126.10:8787")
    card = notifications._feishu_completion_card("标题", "完成", None, "session-1")
    assert card["elements"][1]["actions"][0]["url"] == "http://10.126.126.10:8787/session/session-1"
    monkeypatch.setenv("HERMES_WEBUI_PUBLIC_URL", "http://public.example:8787")
    assert len(notifications._feishu_completion_card("标题", "完成", None, "session-1")["elements"]) == 1


def test_feishu_card_url_cannot_use_credential_or_ipv4_mapped_loopback(notifications, monkeypatch):
    for origin in ("https://safe.example@evil.example", "http://[::ffff:127.0.0.1]:8787", "https://evil.example/path", "https://[broken", "https://good.example:bad"):
        monkeypatch.setenv("HERMES_WEBUI_PUBLIC_URL", origin)
        assert len(notifications._feishu_completion_card("标题", "完成", None, "session-1")["elements"]) == 1


def test_feishu_card_rejection_falls_back_once_to_text(notifications, monkeypatch):
    calls = []
    monkeypatch.setattr(notifications, "_run_feishu_card_process", lambda card, home: calls.append("card") or {"card_rejected": True})
    monkeypatch.setattr(notifications, "_send_via_hermes", lambda channel, text, home: calls.append((channel, text)) or {"success": True})
    result = notifications._send_feishu_completion("Title", "Done", (0, 1), "session-1", None)
    assert result["success"] is True
    assert calls[0] == "card" and calls[1][0] == "feishu"
    assert "运行中  子代理 0 · 后台进程 1" in calls[1][1]


@pytest.mark.parametrize("code", [230001, 230011, 230013, 99991663, None])
def test_feishu_non_card_specific_rejection_never_sends_again(notifications, monkeypatch, code):
    monkeypatch.setattr(notifications, "_run_feishu_card_process", lambda card, home: {"rejected": True, "code": code})
    monkeypatch.setattr(notifications, "_send_via_hermes", lambda *args: pytest.fail("non-card failure resent as text"))
    with pytest.raises(notifications._DeliveryFailure):
        notifications._send_feishu_completion("Title", "Done", None, "session-1", None)


def test_feishu_card_uncertain_error_does_not_duplicate_fallback(notifications, monkeypatch):
    monkeypatch.setattr(notifications, "_run_feishu_card_process", lambda card, home: {"error": "timeout"})
    monkeypatch.setattr(notifications, "_send_via_hermes", lambda *args: pytest.fail("uncertain card retried as text"))
    with pytest.raises(notifications._DeliveryFailure):
        notifications._send_feishu_completion("Title", "Done", None, "session-1", None)


def test_feishu_card_sender_subprocess_reads_only_feishu_environment(notifications, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    recorded = []

    def run(command, **kwargs):
        recorded.append((command, kwargs))
        return SimpleNamespace(stdout=notifications._SENDER_MARKER + json.dumps({"success": True, "message_id": "om_test"}) + "\n")

    monkeypatch.setattr(notifications.subprocess, "run", run)
    result = notifications._run_feishu_card_process({"header": {"title": "safe"}}, None)
    assert result == {"success": True, "message_id": "om_test"}
    command, kwargs = recorded[0]
    assert command[1] == "-I" and "OPENAI_API_KEY" not in kwargs["env"]
    assert json.loads(kwargs["input"])["header"]["title"] == "safe"


def test_feishu_card_sender_executes_isolated_adapter_contract(notifications, monkeypatch, tmp_path):
    root = tmp_path / "agent"
    for parts in [("tools",), ("gateway",), ("plugins",), ("plugins", "platforms"), ("plugins", "platforms", "feishu")]:
        folder = root.joinpath(*parts)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "__init__.py").write_text("", encoding="utf-8")
    (root / "tools" / "send_message_tool.py").write_text(
        "def _resolve_platform_config(name, config): return ('feishu', object(), None, None)\n"
        "def _home_chat_id(config, platform, name): return ('oc_fake', None)\n", encoding="utf-8",
    )
    (root / "gateway" / "config.py").write_text(
        "class Platform: FEISHU='feishu'\n"
        "def load_gateway_config(): return object()\n", encoding="utf-8",
    )
    (root / "plugins" / "platforms" / "feishu" / "adapter.py").write_text(
        "import json\n"
        "def _sdk_domain(name): return name\n"
        "def _load_lark_oapi(): return True\n"
        "class FeishuAdapter:\n"
        " def __init__(self, config): self._domain_name='feishu'\n"
        " def _build_lark_client(self, domain): return object()\n"
        " async def _send_raw_message(self, **kw):\n"
        "  from types import SimpleNamespace\n"
        "  assert kw['chat_id']=='oc_fake' and kw['msg_type']=='interactive'\n"
        "  assert json.loads(kw['payload'])['header']['title']['content']=='Hermes · 回复完成'\n"
        "  return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id='om_fake'))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(notifications, "_sender_environment", lambda channel, home: ({"PATH": os.environ["PATH"]}, root))
    result = notifications._run_feishu_card_process(notifications._feishu_completion_card("标题", "完成", (2, 3), "sid"), None)
    assert result == {"success": True, "message_id": "om_fake"}


def test_feishu_card_sender_reports_exception_class_without_leaking_error(notifications, monkeypatch, tmp_path):
    root = tmp_path / "agent"
    for parts in [("tools",), ("gateway",), ("plugins",), ("plugins", "platforms"), ("plugins", "platforms", "feishu")]:
        folder = root.joinpath(*parts)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "__init__.py").write_text("", encoding="utf-8")
    (root / "tools" / "send_message_tool.py").write_text(
        "def _resolve_platform_config(name, config): return ('feishu', object(), None, None)\n"
        "def _home_chat_id(config, platform, name): return ('oc_fake', None)\n", encoding="utf-8",
    )
    (root / "gateway" / "config.py").write_text(
        "class Platform: FEISHU='feishu'\n"
        "def load_gateway_config(): return object()\n", encoding="utf-8",
    )
    (root / "plugins" / "platforms" / "feishu" / "adapter.py").write_text(
        "def _sdk_domain(name): return name\n"
        "def _load_lark_oapi(): return True\n"
        "class FeishuAdapter:\n"
        " def __init__(self, config): self._domain_name='feishu'\n"
        " def _build_lark_client(self, domain): return object()\n"
        " async def _send_raw_message(self, **kw): raise RuntimeError('secret-on-wire')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(notifications, "_sender_environment", lambda channel, home: ({"PATH": os.environ["PATH"]}, root))
    result = notifications._run_feishu_card_process(notifications._feishu_completion_card("标题", "完成", (0, 0), "sid"), None)
    assert result == {"error": "card sender exception", "error_type": "RuntimeError"}
    assert "secret-on-wire" not in repr(result)


def test_feishu_card_sender_reports_nonzero_process_without_leaking_stderr(notifications, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(notifications.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=7, stdout="", stderr="secret-on-wire"))
    result = notifications._run_feishu_card_process({"header": {"title": "safe"}}, None)
    assert result == {"error": "card sender process failed", "exit_code": 7}
    assert "secret-on-wire" not in repr(result)


def test_feishu_card_send_to_same_profile_uses_only_one_channel_claim(notifications, monkeypatch):
    sent = []
    monkeypatch.setattr(notifications, "_run_feishu_card_process", lambda card, home: sent.append("card") or {"success": True, "message_id": "om_test"})
    monkeypatch.setattr(notifications, "_send_via_hermes", lambda *args: pytest.fail("duplicate plain text"))
    settings = {"completion_notifications_enabled": True, "completion_notification_channels": ["browser", "feishu"]}
    first = notifications.send_completion(settings, session_id="one", stream_id="one", title="标题", text="完成")
    second = notifications.send_completion(settings, session_id="one", stream_id="one", title="标题", text="完成")
    assert first == {"sent": ["feishu"], "failed": []}
    assert second == {"sent": [], "failed": []}
    assert sent == ["card"]


def test_completion_text_has_safe_fallbacks_and_hard_total_limit(notifications):
    fallback = notifications._completion_text("\n\x00", "\t\n")
    assert fallback == (
        "Hermes 回复完成\n会话：未命名会话\n结论摘要：已完成，请返回 WebUI 查看结果。"
        "\n通知生成时当前会话正在进行中子代理数量：未知\n后台进程数量：未知"
    )

    safe_unicode = notifications._completion_text("生产\u202e标题", "完成\u200b✅")
    assert safe_unicode == (
        "Hermes 回复完成\n会话：生产标题\n结论摘要：完成✅"
        "\n通知生成时当前会话正在进行中子代理数量：未知\n后台进程数量：未知"
    )

    bounded = notifications._completion_text("标" * 500, "结论" * 5000)
    assert len(bounded) == 500
    assert bounded.splitlines()[1].startswith("会话：")
    assert bounded.splitlines()[1].endswith("…")
    assert bounded.splitlines()[2].startswith("结论摘要：")
    assert bounded.splitlines()[-2:] == ["通知生成时当前会话正在进行中子代理数量：未知", "后台进程数量：未知"]


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
