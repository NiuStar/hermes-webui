"""Execute the workflow's shell with a Docker boundary double, never a daemon."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/docker-smoke.yml"
CANDIDATE = "sha256:" + "c" * 64


@pytest.mark.parametrize("variant", ["single", "two-container", "three-container"])
@pytest.mark.parametrize("failure", [None, "wrong-config", "wrong-revision", "wrong-runtime", "up-failed"])
def test_smoke_uses_built_image_id_before_up(tmp_path, variant, failure):
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    compose_name = "docker-compose.yml" if variant == "single" else f"docker-compose.{variant}.yml"
    compose_config = yaml.safe_load((ROOT / compose_name).read_text())
    assert compose_config["services"]["hermes-webui"]["image"] == "${HERMES_WEBUI_RUNTIME_IMAGE:-24802117/hermes-webui:latest}"
    step = next(s for s in workflow["jobs"]["smoke"]["steps"] if s.get("name", "").startswith("Smoke ("))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['DOCKER_CALLS'], 'a') as f:
    f.write(json.dumps(args) + '\\n')
if args[:2] == ['image', 'inspect']:
    print(os.environ['REVISION'] if 'Labels' in ' '.join(args) else os.environ['EXPECTED_IMAGE'])
elif 'up' in args and os.environ['FAILURE'] == 'up-failed':
    sys.exit(23)
elif args[0] == 'inspect':
    print(os.environ['ACTUAL_IMAGE'])
elif 'config' in args:
    if '--format' in args:
        image = 'published:wrong' if os.environ['FAILURE'] == 'wrong-config' else os.environ.get('HERMES_WEBUI_RUNTIME_IMAGE', '24802117/hermes-webui:latest')
        print(json.dumps({{'services': {{'hermes-webui': {{'image': image, 'environment': {{'SECRET': 'never-archive'}}}}}}}}))
    else:
        print(os.environ.get('HERMES_WEBUI_RUNTIME_IMAGE', '24802117/hermes-webui:latest'))
elif 'ps' in args and '-q' in args:
    print('fixture-container')
''')
    docker.chmod(0o755)
    curl = bindir / "curl"
    curl.write_text("#!/bin/sh\nexit 0\n")
    curl.chmod(0o755)
    compose = "docker-compose.yml" if variant == "single" else f"docker-compose.{variant}.yml"
    env = {"PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(tmp_path),
           "COMPOSE_FILE": str(ROOT / compose), "PROJECT": f"hermes-smoke-{variant}-fixture",
           "HERMES_HOME": str(tmp_path / "home"), "HERMES_WORKSPACE": str(tmp_path / "work"),
           "HERMES_WEBUI_RUNTIME_IMAGE": "hermes-webui-smoke:fixture", "EXPECTED_IMAGE": CANDIDATE,
           "ACTUAL_IMAGE": "sha256:" + "d" * 64 if failure == "wrong-runtime" else CANDIDATE,
           "GITHUB_SHA": "a" * 40, "REVISION": "b" * 40 if failure == "wrong-revision" else "a" * 40,
           "FAILURE": failure or "", "DOCKER_CALLS": str(tmp_path / "calls.jsonl")}
    script = step["run"].replace("${{ matrix.variant }}", variant)
    run = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert (tmp_path / "ci-evidence/compose.log").is_file()
    assert any("down" in args for args in calls), "cleanup must survive every failure"
    if failure:
        assert run.returncode != 0, run.stdout + run.stderr
        if failure in ("wrong-config", "wrong-revision"):
            assert not any("up" in args for args in calls)
        return
    assert run.returncode == 0, run.stdout + run.stderr
    up = next(args for args in calls if "up" in args)
    assert "--no-build" in up and up[up.index("--pull") + 1] == "never"
    config = json.loads((tmp_path / "ci-evidence/compose-config.json").read_text())
    assert config["services"]["hermes-webui"]["image"] == CANDIDATE
    assert "never-archive" not in (tmp_path / "ci-evidence/compose-config.json").read_text()
    expected_pulls = [] if variant == "single" else ["hermes-agent"] + (["hermes-dashboard"] if variant == "three-container" else [])
    assert [args[-1] for args in calls if "pull" in args] == expected_pulls
    assert any(args[0] == "inspect" for args in calls), "verify actual container image, not just Compose intent"
    assert (tmp_path / "ci-evidence/compose.log").is_file()


def test_workflow_builds_revision_labeled_candidates_for_all_consumers():
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    for job in ("build-image", "state-dir-uid", "smoke"):
        build = next(s for s in workflow["jobs"][job]["steps"] if s.get("uses", "").startswith("docker/build-push-action@"))
        assert build["with"]["tags"] == "hermes-webui-smoke:${{ github.sha }}"
        assert "HERMES_WEBUI_REVISION=${{ github.sha }}" in build["with"]["build-args"]
    steps = workflow["jobs"]["state-dir-uid"]["steps"]
    for step in steps:
        if "docker run " in step.get("run", ""):
            assert "--pull=never" in step["run"]
            assert '"$HERMES_WEBUI_RUNTIME_IMAGE"' in step["run"]
