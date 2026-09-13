#!/usr/bin/env python3
"""Audit or repair derived indexes for large Hermes WebUI session sidecars."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.session_sidecar_maintenance import run_sidecar_maintenance_once


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build disposable byte-offset indexes for large session sidecars. "
            "The authoritative sidecar JSON is never rewritten or deleted."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write missing/stale derived indexes (default is dry-run)",
    )
    parser.add_argument(
        "--threshold-bytes",
        type=int,
        default=10 * 1024 * 1024,
        help="minimum sidecar size to inspect (default: 10485760)",
    )
    args = parser.parse_args()
    result = run_sidecar_maintenance_once(
        threshold_bytes=max(1, args.threshold_bytes),
        dry_run=not args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") in {"ok", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
