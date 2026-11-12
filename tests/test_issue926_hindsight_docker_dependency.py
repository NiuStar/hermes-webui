"""Regression tests for #926 Hindsight dependency in Docker WebUI venv."""
import pathlib


REPO_ROOT = pathlib.Path(__file__).parent.parent
INIT_SH = (REPO_ROOT / "docker_init.bash").read_text(encoding="utf-8")
REQUIREMENTS_TXT = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")


def test_926_docker_init_installs_hindsight_distribution():
    """Docker init must install the PyPI distribution named hindsight-client."""
    assert "uv pip show hindsight-client" in INIT_SH
    assert '"hindsight-client>=0.4.22"' in INIT_SH
    assert 'uv pip install "${_hindsight_client_requirement}"' in INIT_SH


def test_926_hindsight_install_runs_after_fast_restart_guard(tmp_path):
    """Existing Docker venvs with .deps_installed must still get hindsight-client."""
    deps_guard_pos = INIT_SH.find("if [ -f /app/venv/.deps_installed ]; then")
    assert deps_guard_pos != -1, ".deps_installed fast-restart guard not found"

    import subprocess

    # Execute the real fast-restart branch without hard-coding adjacency to
    # other optional dependency checks (Feishu now runs between fi and this call).
    block = INIT_SH[deps_guard_pos:INIT_SH.index('echo ""; echo "== Running hermes-webui"', deps_guard_pos)]
    marker = tmp_path / ".deps_installed"
    marker.touch()
    block = block.replace("/app/venv/.deps_installed", str(marker))
    result = subprocess.run(
        ["bash", "-c", 'set -e\nensure_feishu_sender_dependency() { echo FEISHU; }\nensure_hindsight_client_docker_dependency() { echo HINDSIGHT; }\n' + block],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines().count("HINDSIGHT") == 1


def test_926_hindsight_dependency_stays_docker_specific():
    """Local non-Docker bootstrap should not install optional memory clients."""
    assert "hindsight-client" not in REQUIREMENTS_TXT
