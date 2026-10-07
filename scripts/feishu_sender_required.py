#!/usr/bin/env python3
"""Exit zero if any WebUI profile may send Feishu completion notices.

Only inspects local configuration; never prints credentials or recipient IDs.
Called by docker_init.bash before the WebUI worker starts.
"""
from __future__ import annotations

import os
from pathlib import Path


def _has_feishu_config(home: Path) -> bool:
    from api.completion_notifications import _platform_configured

    source = {"HERMES_HOME": str(home), "HERMES_WEBUI_AGENT_DIR": os.getenv("HERMES_WEBUI_AGENT_DIR", "")}
    # For the default home, configuration may also come from process env.
    if home == Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser():
        source.update({k: v for k, v in os.environ.items() if k.startswith("FEISHU_")})
    return _platform_configured("feishu", source, require_sender=False)


def main() -> int:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
    if _has_feishu_config(home):
        return 0
    profiles = home / "profiles"
    if profiles.is_dir():
        for child in profiles.iterdir():
            if child.is_dir() and not child.is_symlink() and _has_feishu_config(child):
                return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
