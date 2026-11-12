"""The init shell must explicitly prepare the mounted Hermes-home boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_init_repairs_hermes_mount_separately_from_home(tmp_path):
    init = (ROOT / "docker_init.bash").read_text()
    block = init[init.index("chown_home_hermeswebui() {"):init.index("# The production image does not ship sudo.")]
    mount = tmp_path / "home/.hermes"
    (mount / "hermes-agent").mkdir(parents=True)
    calls = tmp_path / "calls.jsonl"
    recorder = tmp_path / "record.py"
    recorder.write_text("import json, os, sys\nwith open(os.environ['CALLS'], 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n")
    block = block.replace("/usr/local/bin/python", sys.executable).replace("/apptoo/api/docker_home_ownership.py", str(recorder)).replace("/home/hermeswebui", str(mount.parent))
    run = subprocess.run(["bash", "-c", "set -e\n" + block + "\nchown_home_hermeswebui\n"], env={"PATH": os.environ["PATH"], "WANTED_UID": "1000", "WANTED_GID": "1000", "CALLS": str(calls)}, capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stdout + run.stderr
    actual = [json.loads(line) for line in calls.read_text().splitlines()]
    assert actual == [[str(mount.parent), "1000", "1000"], [str(mount), "1000", "1000"]]
