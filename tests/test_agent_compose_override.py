"""Agent updater opt-in Compose contract (no Docker or production side effects)."""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
OVERRIDE = ROOT / 'docker-compose.agent-update.yml'


def test_agent_update_override_is_explicit_and_fails_closed():
    text = OVERRIDE.read_text()
    cfg = yaml.safe_load(text)
    services = cfg['services']
    web = services['hermes-webui']
    updater = services['hermes-webui-updater']
    assert '${HERMES_WEBUI_ACCEPTED_IMAGE:?' in text
    assert web['image'] == updater['image']
    assert '@sha256:' in web['image'] or '${HERMES_WEBUI_ACCEPTED_IMAGE:?' in web['image']
    assert updater['profiles'] == ['self-update']
    env = updater['environment']
    assert env['HERMES_WEBUI_DOCKER_SELF_UPDATE'] == '1'
    assert env['HERMES_WEBUI_AGENT_RELEASE_FILE'] == '/run/hermes-agent-release/release.json'
    assert not any(key.startswith('HERMES_WEBUI_AGENT_MANIFEST_') for key in env)
    assert 'HERMES_WEBUI_AGENT_COMMIT' not in env
    assert env['HERMES_WEBUI_DOCKER_IMAGE'].endswith(':?required}')
    assert updater['network_mode'] == 'none'
    assert updater['read_only'] is True
    mounts = updater['volumes']
    assert len(mounts) == 1
    assert mounts[0]['type'] == 'bind'
    assert mounts[0]['source'].endswith(':?required}')
    assert mounts[0]['target'] == '/run/hermes-agent-release'
    assert mounts[0]['read_only'] is True


def test_base_compose_remains_unprovisioned():
    cfg = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())
    updater = cfg['services']['hermes-webui-updater']
    assert updater['profiles'] == ['self-update']
    assert 'HERMES_WEBUI_AGENT_MANIFEST_PATH' not in '\n'.join(updater['environment'])
