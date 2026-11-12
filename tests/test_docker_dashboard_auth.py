"""Three-container dashboard must use the upstream authenticated-bind contract."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_forwards_session_auth_without_insecure_bypass():
    compose = yaml.safe_load((ROOT / "docker-compose.three-container.yml").read_text())
    dashboard = compose["services"]["hermes-dashboard"]
    assert "HERMES_DASHBOARD_SESSION_TOKEN=${HERMES_DASHBOARD_SESSION_TOKEN:-}" in dashboard["environment"]
    assert "--insecure" not in dashboard["command"]
    assert "--host 0.0.0.0" in dashboard["command"], "container listener must remain reachable via published port"


def test_smoke_supplies_disposable_dashboard_auth():
    workflow = yaml.load((ROOT / ".github/workflows/docker-smoke.yml").read_text(), Loader=yaml.BaseLoader)
    step = next(s for s in workflow["jobs"]["smoke"]["steps"] if s.get("name", "").startswith("Smoke ("))
    assert "export HERMES_DASHBOARD_SESSION_TOKEN=" in step["run"]
    assert "secrets.token_urlsafe" in step["run"]
