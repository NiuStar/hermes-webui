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
    for key in ('HERMES_WEBUI_AGENT_COMMIT', 'HERMES_WEBUI_AGENT_MANIFEST_PATH',
                'HERMES_WEBUI_AGENT_MANIFEST_SIGNATURE_PATH',
                'HERMES_WEBUI_AGENT_MANIFEST_PUBKEY',
                'HERMES_WEBUI_AGENT_MANIFEST_PUBKEY_SHA256'):
        assert key in env
    assert env['HERMES_WEBUI_AGENT_COMMIT'].endswith(':?required}')
    assert env['HERMES_WEBUI_AGENT_MANIFEST_PUBKEY_SHA256'].endswith(':?required}')
    assert env['HERMES_WEBUI_DOCKER_IMAGE'].endswith(':?required}')
    assert updater['network_mode'] == 'none'
    assert updater['read_only'] is True
    mounts = updater['volumes']
    for name in ('manifest.json', 'manifest.json.sig', 'publisher.pub'):
        matching = [v for v in mounts if v['target'] == '/run/hermes-agent-release/' + name]
        assert len(matching) == 1 and matching[0]['read_only'] is True
        assert matching[0]['type'] == 'bind'
    assert all(not v.get('read_only') is False for v in mounts if isinstance(v, dict))


def test_base_compose_remains_unprovisioned():
    cfg = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())
    updater = cfg['services']['hermes-webui-updater']
    assert updater['profiles'] == ['self-update']
    assert 'HERMES_WEBUI_AGENT_MANIFEST_PATH' not in '\n'.join(updater['environment'])
