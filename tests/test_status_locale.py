"""New status and completion copy must be localized, not silently fall back."""

from pathlib import Path
import re

import pytest

from tests.test_zh_hant_locale import locale_block, value_map


REPO = Path(__file__).resolve().parent.parent
SOURCE = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
LOCALES = re.findall(r"^  ([A-Za-z_]+|'[^']+'):\s*\{", SOURCE, re.MULTILINE)
STATUS_KEYS = {
    "active_sessions_count",
    "active_sessions_unknown",
    "active_sessions_titlebar",
    "active_sessions_ended",
    "active_background_running",
    "active_delegation_running",
    "active_process_running",
    "active_auxiliary_ended",
    "active_background_done",
    "active_background_unknown",
    "active_background_result_unavailable",
    "settings_label_completion_notifications",
    "settings_desc_completion_notifications",
    "settings_desc_completion_channels",
    "settings_completion_channel_browser",
    "settings_completion_channel_weixin",
    "settings_completion_channel_wecom",
    "settings_completion_channel_feishu",
    "settings_webui_unpublished",
}
ENGLISH = value_map(locale_block(SOURCE, "\n  en: {"))
STATUS_KEYS.update(
    key for key in ENGLISH
    if key.startswith(("update_progress_", "settings_agent_"))
)


@pytest.mark.parametrize("locale", LOCALES)
def test_status_locale_owns_translations_and_preserves_placeholders(locale):
    block = locale_block(SOURCE, f"\n  {locale}: {{")
    values = value_map(block)
    entries = re.findall(r"^\s+([a-zA-Z0-9_]+):", block, re.MULTILINE)
    # Product names and the German/Italian word Browser are intentionally shared.
    shared_names = {
        "settings_completion_channel_weixin",
        "settings_completion_channel_wecom",
        "settings_completion_channel_feishu",
    }
    if locale in {"de", "it"}:
        shared_names.add("settings_completion_channel_browser")

    for key in sorted(STATUS_KEYS):
        assert entries.count(key) == 1, (locale, key)
        assert sorted(re.findall(r"\{\d+\}", values[key])) == sorted(
            re.findall(r"\{\d+\}", ENGLISH[key])
        ), (locale, key)
        if locale != "en" and key not in shared_names:
            assert values[key] != ENGLISH[key], (locale, key, "untranslated")
        # These suffixes are appended to other sentences at runtime.
        if key in {
            "update_progress_restored",
            "update_progress_unchanged",
            "update_progress_rollback_attention",
        }:
            assert values[key].startswith("' "), (locale, key)
