"""Regression contracts for completion notification settings."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_settings_expose_completion_notification_controls():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    js = (ROOT / "static/panels.js").read_text(encoding="utf-8")
    boot = (ROOT / "static/boot.js").read_text(encoding="utf-8")
    messages = (ROOT / "static/messages.js").read_text(encoding="utf-8")
    assert "settingsCompletionNotificationsEnabled" in html
    assert 'name="completionNotificationChannel"' in html
    assert 'value="weixin"' in html
    assert 'value="wecom"' in html
    assert 'value="wechat_work"' not in html
    assert "completion_notification_channels_status" in js
    assert "completion_notification_channels" in js
    assert "const channelConfigured=input.value==='browser'||!!completionStatus[input.value]" in js
    assert "input.disabled=!channelConfigured" in js
    assert "if(allCompletionChannels.length) payload.completion_notification_channels" in js
    assert "window._completionNotificationChannels" in boot
    assert "_metadataOnlyCompletionNotice" in messages
    assert "Return to WebUI to view the response." in messages


def test_completion_settings_are_applied_without_exposing_credentials():
    js = (ROOT / "static/panels.js").read_text(encoding="utf-8")
    assert "window._completionNotificationsEnabled" in js
    assert "settings_completion_notification" not in js.lower()
    assert "WEBHOOK" not in js
