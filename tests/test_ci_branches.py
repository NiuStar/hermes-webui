"""Offline branch-admission regressions for the CI workflows (no GitHub writes)."""

from fnmatch import fnmatchcase
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
BRANCH_FILTERED = (
    "tests.yml",
    "browser-smoke.yml",
    "conversation-lifecycle.yml",
    "docker-smoke.yml",
    "docs-ci.yml",
)
REPAIR_BRANCH = "fix/agent-release-ancestry-depth-20261111"


def _workflow(name):
    # BaseLoader keeps GitHub's `on` key literal (YAML 1.1 treats it as True).
    return yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


@pytest.mark.parametrize("name", BRANCH_FILTERED)
@pytest.mark.parametrize("event", ("push", "pull_request"))
@pytest.mark.parametrize("branch", ("main", "master", REPAIR_BRANCH, "fix/ci/nested"))
def test_ci_admits_primary_legacy_and_repair_branches(name, event, branch):
    filters = _workflow(name)["on"][event]["branches"]
    assert any(fnmatchcase(branch, pattern) for pattern in filters), (name, event, branch)


@pytest.mark.parametrize("name", BRANCH_FILTERED)
@pytest.mark.parametrize("event", ("push", "pull_request"))
def test_ci_branch_repair_does_not_admit_unrelated_branches(name, event):
    filters = _workflow(name)["on"][event]["branches"]
    assert not any(fnmatchcase("feature/unrelated", pattern) for pattern in filters)


def test_native_windows_prs_remain_unrestricted_by_branch():
    assert "branches" not in _workflow("native-windows-startup.yml")["on"]["pull_request"]


def test_full_test_matrix_and_required_execution_are_preserved():
    jobs = _workflow("tests.yml")["jobs"]
    assert jobs["test"]["strategy"] == {
        "fail-fast": "false",
        "matrix": {"python-version": ["3.11", "3.12", "3.13"], "shard": ["0", "1", "2", "3", "4"]},
    }
    assert jobs["test"]["if"] == "${{ always() }}"
    test_step = next(step for step in jobs["test"]["steps"] if step.get("name", "").startswith("Run tests"))
    assert test_step["run"] == "pytest tests/ -v --timeout=60 --shard-id=${{ matrix.shard }} --num-shards=5"
    assert test_step["if"] == "needs.changes.outputs.docs_only != 'true'"


def test_diff_workflows_share_event_aware_base_selection():
    expected = "${{ github.base_ref || (github.ref_name == 'master' && 'master' || 'main') }}"
    for name in ("tests.yml", "browser-smoke.yml", "docs-ci.yml"):
        assert _workflow(name)["env"]["CI_BASE_BRANCH"] == expected


@pytest.mark.parametrize("base_branch", ("main", "master"))
def test_lint_fetch_and_gate_use_same_available_base(tmp_path, base_branch):
    """Execute the workflow shell against a local single-branch remote, then catch F821."""
    def git(*args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()

    remote = tmp_path / "remote"
    remote.mkdir()
    git("init", "-b", base_branch, cwd=remote)
    git("config", "user.name", "CI fixture", cwd=remote)
    git("config", "user.email", "ci@example.invalid", cwd=remote)
    (remote / "example.py").write_text("value = 1\n", encoding="utf-8")
    git("add", ".", cwd=remote)
    git("commit", "-m", "base", cwd=remote)
    base_sha = git("rev-parse", "HEAD", cwd=remote)

    checkout = tmp_path / "checkout"
    git("clone", "--single-branch", str(remote), str(checkout), cwd=tmp_path)
    git("config", "user.name", "CI fixture", cwd=checkout)
    git("config", "user.email", "ci@example.invalid", cwd=checkout)
    git("checkout", "-b", REPAIR_BRANCH, cwd=checkout)
    (checkout / "example.py").write_text("value = missing_ci_symbol\n", encoding="utf-8")
    git("add", ".", cwd=checkout)
    git("commit", "-m", "new lint violation", cwd=checkout)
    # Match checkout configurations where the remote-tracking base is absent.
    git("update-ref", "-d", f"refs/remotes/origin/{base_branch}", cwd=checkout)
    (checkout / "scripts").mkdir()
    (checkout / "scripts" / "ruff_lint.py").write_bytes((ROOT / "scripts" / "ruff_lint.py").read_bytes())
    (checkout / "pyproject.toml").write_bytes((ROOT / "pyproject.toml").read_bytes())

    lint = _workflow("tests.yml")["jobs"]["lint"]
    fetch = next(step for step in lint["steps"] if step.get("name", "").startswith("Ensure origin/"))
    gate = next(step for step in lint["steps"] if step.get("name", "").startswith("Ruff forward gate"))
    env = {**os.environ, "CI_BASE_BRANCH": base_branch, "PATH": str(ROOT / ".venv" / "bin") + os.pathsep + os.environ["PATH"]}
    result = subprocess.run(["bash", "-e", "-c", fetch["run"]], cwd=checkout, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert git("rev-parse", f"origin/{base_branch}", cwd=checkout) == base_sha
    result = subprocess.run(["bash", "-e", "-c", gate["run"]], cwd=checkout, env=env, text=True, capture_output=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "F821" in result.stdout and "missing_ci_symbol" in result.stdout
    assert f"diff vs origin/{base_branch}" in result.stdout

    # A missing base must stop the job before the diff tool can mistake it for no changes.
    env["CI_BASE_BRANCH"] = "missing-base"
    result = subprocess.run(["bash", "-e", "-c", fetch["run"]], cwd=checkout, env=env, text=True, capture_output=True)
    assert result.returncode != 0
