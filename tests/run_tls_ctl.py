#!/usr/bin/env python3
"""Run TLS/ctl tests in private state with Linux orphan reaping.

Uses scripts/test.sh (and its supported repo venv), never pytest in this process.
Requires real ps/curl/openssl/bash on PATH; no host packages are installed. On
minimal containers supply procps' bin directory and library path in the caller.
Evidence and temporary state are retained under --output, which must be new.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


REPO = Path(__file__).resolve().parents[1]


def reap_adopted_children(exclude: int) -> int:
    """Reap only this runner's dead children, leaving Popen's child to Popen."""
    children = Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()
    reaped = 0
    for raw in children:
        pid = int(raw)
        if pid == exclude:
            continue
        try:
            ended, _ = os.waitpid(pid, os.WNOHANG)
            reaped += bool(ended)
        except ChildProcessError:
            pass
    return reaped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--agent-dir", type=Path, required=True)
    parser.add_argument("nodes", nargs="*", default=[
        "tests/test_tls_aware_probe.py", "tests/test_ctl_script.py",
    ])
    args = parser.parse_args()
    if not sys.platform.startswith("linux"):
        parser.error("this private subreaper runner requires Linux; use scripts/test.sh on other hosts")
    for command in ("ps", "bash", "curl", "openssl"):
        if not shutil.which(command):
            parser.error(f"required test dependency not on PATH: {command}")
    # ctl's PID identity and uptime checks need these ps options, not a mock.
    subprocess.run(["ps", "-p", str(os.getpid()), "-o", "args="], check=True,
                   stdout=subprocess.DEVNULL)
    agent = args.agent_dir.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    env = {k: v for k, v in os.environ.items()
           if not any(s in k.upper() for s in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))
           and not k.startswith(("HERMES", "OPENAI", "ANTHROPIC"))
           and k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    env.update(
        HOME=str(output / "home"), HERMES_HOME=str(output / "agent-home"),
        HERMES_BASE_HOME=str(output / "agent-home"),
        HERMES_CONFIG_PATH=str(output / "agent-home/config.yaml"),
        HERMES_WEBUI_STATE_DIR=str(output / "state"),
        HERMES_WEBUI_TEST_STATE_DIR=str(output / "test-state"),
        HERMES_WEBUI_DEFAULT_WORKSPACE=str(output / "workspace"),
        HERMES_WEBUI_AGENT_DIR=str(agent),
        HERMES_WEBUI_PYTHON=str(REPO / ".venv/bin/python"),
        HERMES_WEBUI_DISABLE_SESSION_DELETE="1",
        HERMES_WEBUI_TEST_NETWORK_BLOCK="1", AWS_EC2_METADATA_DISABLED="true",
        PYTHONPATH=os.pathsep.join((str(REPO), str(agent))),
        PATH=str(REPO / ".venv/bin") + os.pathsep + env.get("PATH", ""),
        PYTHONUNBUFFERED="1",
    )
    for key in ("HOME", "HERMES_HOME", "HERMES_WEBUI_STATE_DIR",
                "HERMES_WEBUI_TEST_STATE_DIR", "HERMES_WEBUI_DEFAULT_WORKSPACE"):
        Path(env[key]).mkdir(parents=True)
    # Private ancestor only: no global SIGCHLD policy, host signals, or pytest
    # waitpid races. The runner lives until its adopted test children exit.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot enable private child subreaper")
    command = ["./scripts/test.sh", *args.nodes, "-vv", "-ra", "--timeout=60",
               "-p", "no:cacheprovider", f"--basetemp={output}/tmp",
               f"--junitxml={output}/results.xml"]
    reaped = 0
    started = time.monotonic()
    with (output / "run.log").open("w") as log:
        proc = subprocess.Popen(command, cwd=REPO, env=env, stdout=log,
                                stderr=subprocess.STDOUT)
        while proc.poll() is None:
            reaped += reap_adopted_children(proc.pid)
            time.sleep(0.02)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            reaped += reap_adopted_children(proc.pid)
            if not Path(f"/proc/self/task/{os.getpid()}/children").read_text().strip():
                break
            time.sleep(0.02)
    remaining = Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()
    result = dict(command=command, returncode=proc.returncode,
                  seconds=time.monotonic() - started, reaped=reaped,
                  remaining_children=remaining)
    (output / "status.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    # An unreleased test child is a failure, not a silently accepted zombie.
    return proc.returncode or (1 if remaining else 0)


if __name__ == "__main__":
    raise SystemExit(main())
