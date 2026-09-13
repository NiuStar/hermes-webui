"""Server-side notifications for completed WebUI turns.

The WebUI owns the completion decision. Non-browser delivery is delegated to
Hermes Agent's official ``send_message`` implementation and its configured home
channel. Credentials never cross the WebUI API boundary.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from api.config import STATE_DIR, _AGENT_DIR

logger = logging.getLogger(__name__)

CHANNELS = ("browser", "weixin", "wecom", "feishu")
_CHANNEL_ALIASES = {"wechat_work": "wecom"}
_PLATFORM_REQUIRED_ENV = {
    "weixin": ("WEIXIN_TOKEN", "WEIXIN_ACCOUNT_ID", "WEIXIN_HOME_CHANNEL"),
    "wecom": ("WECOM_BOT_ID", "WECOM_SECRET", "WECOM_HOME_CHANNEL"),
    "feishu": ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_HOME_CHANNEL"),
}
_PLATFORM_ENV_PREFIX = {
    "weixin": "WEIXIN_",
    "wecom": "WECOM_",
    "feishu": "FEISHU_",
}
_PLATFORM_CONFIG_FIELDS = {
    "weixin": (("token",), ("extra", "account_id"), ("home_channel", "chat_id")),
    "wecom": (("extra", "bot_id"), ("extra", "secret"), ("home_channel", "chat_id")),
    "feishu": (("extra", "app_id"), ("extra", "app_secret"), ("home_channel", "chat_id")),
}
_MAX_PREVIEW_CHARS = 500
_MAX_STATE_ENTRIES = 512
_STATE_LOCK = threading.Lock()
_IN_FLIGHT: set[tuple[str, str, str]] = set()
_SENDER_MARKER = "HERMES_COMPLETION_NOTIFICATION_RESULT="
_RETRY_AFTER_RE = re.compile(r"(?:cooldown active for|retry after)\s+([0-9]+(?:\.[0-9]+)?)s?", re.IGNORECASE)
_SUBPROCESS_BASE_ENV = {
    "HOME",
    "HERMES_AGENT_DIR",
    "HERMES_HOME",
    "HERMES_WEBUI_AGENT_DIR",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}


def _hermes_home(source: Mapping[str, str] | None = None) -> Path:
    env = os.environ if source is None else source
    configured = str(env.get("HERMES_HOME", "") or "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def _agent_root(source: Mapping[str, str] | None = None) -> Path:
    env = os.environ if source is None else source
    configured = str(
        env.get("HERMES_WEBUI_AGENT_DIR")
        or env.get("HERMES_AGENT_DIR")
        or ""
    ).strip()
    if configured:
        return Path(configured).expanduser()
    for candidate in (Path(_AGENT_DIR), _hermes_home(env) / "hermes-agent"):
        if (candidate / "tools" / "send_message_tool.py").is_file():
            return candidate
    return _hermes_home(env) / "hermes-agent"


def _read_dotenv(path: Path) -> dict[str, str]:
    """Read simple Hermes dotenv assignments without mutating process env."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[:1] in {"'", '"'} and value[-1:] == value[:1]:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _nested_value(payload: Mapping[str, Any], path: tuple[str, ...]) -> str:
    current: Any = payload
    for part in path:
        if not isinstance(current, Mapping):
            return ""
        current = current.get(part)
    if isinstance(current, Mapping):
        current = current.get("chat_id")
    return str(current or "").strip()


def _yaml_platform_config(channel: str, home: Path) -> dict[str, Any]:
    try:
        import yaml

        payload = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, TypeError):
        return {}
    platforms = payload.get("platforms") if isinstance(payload, Mapping) else None
    config = platforms.get(channel) if isinstance(platforms, Mapping) else None
    return dict(config) if isinstance(config, Mapping) else {}


def _configuration_source(source: Mapping[str, str] | None = None) -> tuple[dict[str, str], Path]:
    if source is not None:
        source_values = {str(k): str(v) for k, v in source.items()}
        env = _read_dotenv(_hermes_home(source_values) / ".env")
        env.update({key: value for key, value in source_values.items() if value.strip()})
        return env, _agent_root(env)
    env = _read_dotenv(_hermes_home() / ".env")
    env.update({str(k): str(v) for k, v in os.environ.items()})
    return env, _agent_root(env)


def _platform_configured(channel: str, source: Mapping[str, str] | None = None) -> bool:
    env, agent_root = _configuration_source(source)
    if not (agent_root / "tools" / "send_message_tool.py").is_file():
        return False
    required = _PLATFORM_REQUIRED_ENV[channel]
    if all(str(env.get(key, "") or "").strip() for key in required):
        return True
    config = _yaml_platform_config(channel, _hermes_home(env))
    if not config or config.get("enabled") is False:
        return False
    return all(_nested_value(config, path) for path in _PLATFORM_CONFIG_FIELDS[channel])


def _state_path(hermes_home: str | Path | None = None) -> Path:
    configured = os.environ.get("HERMES_WEBUI_STATE_DIR", "").strip()
    if hermes_home is not None:
        return Path(hermes_home).expanduser() / "webui" / "completion_notifications.json"
    return Path(configured).expanduser() / "completion_notifications.json" if configured else STATE_DIR / "completion_notifications.json"


def public_status(hermes_home: str | Path | None = None) -> dict[str, Any]:
    """Return browser-safe channel status; never return credentials or targets."""
    configured = {"browser": True}
    source = None
    if hermes_home is not None:
        source = {
            "HERMES_HOME": str(Path(hermes_home).expanduser()),
            "HERMES_WEBUI_AGENT_DIR": str(_agent_root()),
        }
    configured.update({channel: _platform_configured(channel, source) for channel in CHANNELS if channel != "browser"})
    return {"enabled": False, "channels": list(CHANNELS), "configured": configured}


def normalize_settings(settings: dict[str, Any], *, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Validate and normalize completion notification settings."""
    enabled = bool(settings.get("completion_notifications_enabled", False))
    raw_channels = settings.get("completion_notification_channels", ["browser"])
    if not isinstance(raw_channels, list):
        raise ValueError("completion notification channels must be a list")
    channels: list[str] = []
    for value in raw_channels:
        channel = _CHANNEL_ALIASES.get(str(value or "").strip().lower(), str(value or "").strip().lower())
        if channel not in CHANNELS:
            raise ValueError(f"Unsupported completion notification channel: {channel or 'empty'}")
        if channel not in channels:
            channels.append(channel)
    if enabled and not channels:
        raise ValueError("At least one completion notification channel is required")
    for channel in channels:
        if enabled and channel != "browser" and not _platform_configured(channel, environ):
            raise ValueError(f"Completion notification channel {channel} is not configured")
    return {
        "completion_notifications_enabled": enabled,
        "completion_notification_channels": channels or ["browser"],
    }


def _read_state(hermes_home: str | Path | None = None) -> dict[str, list[str]]:
    try:
        payload = json.loads(_state_path(hermes_home).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(state: dict[str, list[str]], hermes_home: str | Path | None = None) -> None:
    path = _state_path(hermes_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _claimable_channels(key: str, channels: list[str], hermes_home: str | Path | None = None) -> list[str]:
    scope = str(Path(hermes_home).expanduser()) if hermes_home is not None else "default"
    with _STATE_LOCK:
        state = _read_state(hermes_home)
        sent = set(state.get(key) or [])
        pending = []
        for channel in channels:
            claim = (scope, key, channel)
            if channel != "browser" and channel not in sent and claim not in _IN_FLIGHT:
                _IN_FLIGHT.add(claim)
                pending.append(channel)
        return pending


def _mark_sent(key: str, channel: str, hermes_home: str | Path | None = None) -> None:
    scope = str(Path(hermes_home).expanduser()) if hermes_home is not None else "default"
    with _STATE_LOCK:
        _IN_FLIGHT.discard((scope, key, channel))
        state = _read_state(hermes_home)
        sent = list(state.get(key) or [])
        if channel not in sent:
            sent.append(channel)
        state[key] = sent
        if len(state) > _MAX_STATE_ENTRIES:
            state = dict(list(state.items())[-_MAX_STATE_ENTRIES:])
        _write_state(state, hermes_home)


def _completion_text(title: str, text: str) -> str:
    return "Hermes response complete\nReturn to WebUI to view the response."


class _DeliveryFailure(RuntimeError):
    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def _sender_environment(channel: str, hermes_home: str | Path | None = None) -> tuple[dict[str, str], Path]:
    home = Path(hermes_home).expanduser() if hermes_home is not None else _hermes_home()
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _SUBPROCESS_BASE_ENV
    }
    dotenv = _read_dotenv(home / ".env")
    prefix = _PLATFORM_ENV_PREFIX[channel]
    env.update({key: value for key, value in dotenv.items() if key.startswith(prefix)})
    try:
        is_process_default_home = home.resolve() == _hermes_home().resolve()
    except OSError:
        is_process_default_home = home == _hermes_home()
    if hermes_home is None or is_process_default_home:
        env.update({key: value for key, value in os.environ.items() if key.startswith(prefix)})
    agent_root = _agent_root(env)
    env["PYTHONPATH"] = str(agent_root)
    env["HERMES_HOME"] = str(home)
    return env, agent_root


def _run_sender_process(channel: str, message: str, hermes_home: str | Path | None = None) -> str:
    env, agent_root = _sender_environment(channel, hermes_home)
    if not (agent_root / "tools" / "send_message_tool.py").is_file():
        return json.dumps({"error": "Hermes Agent sender is unavailable"})
    script = (
        "import json\n"
        "from tools.send_message_tool import send_message_tool\n"
        "args=json.loads(input())\n"
        "result=send_message_tool(args)\n"
        f"print({_SENDER_MARKER!r} + (result if isinstance(result,str) else json.dumps(result)))\n"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            input=json.dumps({"action": "send", "target": channel, "message": message}) + "\n",
            text=True,
            capture_output=True,
            timeout=45,
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return json.dumps({"error": "Hermes Agent sender process failed"})
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(_SENDER_MARKER):
            return line[len(_SENDER_MARKER):]
    return json.dumps({"error": "Hermes Agent sender returned no result"})


def _send_via_hermes(channel: str, message: str, hermes_home: str | Path | None = None) -> dict[str, Any]:
    raw = _run_sender_process(channel, message, hermes_home)
    try:
        result = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Hermes Agent sender returned invalid data") from exc
    if not isinstance(result, dict) or result.get("success") is not True or result.get("error"):
        error_text = str(result.get("error") or "") if isinstance(result, dict) else ""
        match = _RETRY_AFTER_RE.search(error_text)
        retry_after = min(max(float(match.group(1)), 0.0), 60.0) if match else None
        raise _DeliveryFailure("Hermes Agent rejected notification", retry_after=retry_after)
    return result


def send_completion(settings: dict[str, Any], *, session_id: str, stream_id: str, title: str, text: str, hermes_home: str | Path | None = None) -> dict[str, Any]:
    """Send configured platform notifications once; browser is client-delivered."""
    validation_env = None
    if hermes_home is not None:
        validation_env = {
            "HERMES_HOME": str(Path(hermes_home).expanduser()),
            "HERMES_WEBUI_AGENT_DIR": str(_agent_root()),
        }
    normalized = normalize_settings(settings, environ=validation_env)
    if not normalized["completion_notifications_enabled"]:
        return {"sent": [], "skipped": "disabled"}
    key = f"{str(session_id).strip()}:{str(stream_id).strip()}"
    message = _completion_text(title, text)
    result: dict[str, Any] = {"sent": [], "failed": []}
    for channel in _claimable_channels(key, normalized["completion_notification_channels"], hermes_home):
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                _send_via_hermes(channel, message, hermes_home)
                _mark_sent(key, channel, hermes_home)
                result["sent"].append(channel)
                last_error = None
                break
            except Exception as exc:  # delivery failure must not break the chat turn
                last_error = exc
                if attempt < 2:
                    retry_after = getattr(exc, "retry_after", None)
                    time.sleep(retry_after if retry_after is not None else 1.0 * (attempt + 1))
        if last_error is not None:
            scope = str(Path(hermes_home).expanduser()) if hermes_home is not None else "default"
            with _STATE_LOCK:
                _IN_FLIGHT.discard((scope, key, channel))
            logger.warning("Completion notification failed for %s: %s", channel, type(last_error).__name__)
            result["failed"].append(channel)
    return result


def notify_turn_terminal(settings: dict[str, Any], *, session_id: str, stream_id: str, status: str, title: str, text: str, hermes_home: str | Path | None = None) -> dict[str, Any]:
    if str(status or "").strip().lower() not in {"completed", "succeeded", "success"}:
        return {"sent": [], "skipped": "terminal_status"}
    return send_completion(settings, session_id=session_id, stream_id=stream_id, title=title, text=text, hermes_home=hermes_home)


def _notification_worker(**kwargs: Any) -> None:
    try:
        notify_turn_terminal(**kwargs)
    except Exception as exc:
        logger.warning("Completion notification worker failed: %s", type(exc).__name__)


def dispatch_completed_turn(settings: dict[str, Any], *, session_id: str, stream_id: str, title: str, text: str, hermes_home: str | Path | None = None) -> None:
    """Run delivery off the streaming worker so a slow platform cannot delay SSE."""
    threading.Thread(
        target=_notification_worker,
        kwargs={"settings": settings, "session_id": session_id, "stream_id": stream_id, "status": "completed", "title": title, "text": text, "hermes_home": hermes_home},
        daemon=True,
        name="completion-notification",
    ).start()
