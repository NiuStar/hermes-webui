"""Real shell/file copies against private state; no container or host mutation."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.geteuid() == 0, reason="permission-denied reproduction requires an unprivileged user")
@pytest.mark.parametrize("use_rsync", [True, False])
@pytest.mark.parametrize("blocked", [".install.lock", "required.py"])
def test_source_staging_ignores_private_installer_lock(tmp_path, use_rsync, blocked):
    init = (ROOT / "docker_init.bash").read_text()
    block = init[init.index('    _stage_src="/app/hermes-agent-src"'):init.index('    uv pip install -e "$_stage_src[all]"')]
    source = tmp_path / "source"
    (source / "tools").mkdir(parents=True)
    (source / "tools/code.py").write_text("VALUE = 1\n")
    (source / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    lock = source / "tools" / blocked
    lock.write_text("private installer lock")
    lock.chmod(0)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # Fallback command lookup excludes rsync but executes the real copy tools.
    for command in ("rm", "mkdir", "cp", "chmod", "find", "tar") + (("rsync",) if use_rsync else ()):
        target = shutil.which(command)
        assert target, command
        (bindir / command).symlink_to(target)
    stage = tmp_path / "stage"
    block = block.replace("/app/hermes-agent-src", str(stage))
    script = 'set -e\nerror_exit() { echo "$*" >&2; exit 1; }\n_agent_src="$SOURCE"\n' + block
    try:
        run = subprocess.run([shutil.which("bash"), "-c", script], env={"PATH": str(bindir), "SOURCE": str(source)}, capture_output=True, text=True, timeout=20)
        if blocked != ".install.lock":
            assert run.returncode != 0, "unreadable required source must still fail closed"
            return
        assert run.returncode == 0, run.stdout + run.stderr
        assert (stage / "tools/code.py").read_bytes() == (source / "tools/code.py").read_bytes()
        assert not (stage / "tools/.install.lock").exists()
        assert lock.stat().st_mode & 0o777 == 0, "never chmod the read-only source"
    finally:
        lock.chmod(0o600)
