from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT = (ROOT / 'docker_init.bash').read_text(encoding='utf-8')


def test_webui_init_does_not_grant_docker_socket_access():
    assert 'hostdocker' not in INIT
    assert "stat -c '%g' /var/run/docker.sock" not in INIT


def test_all_compose_files_isolate_docker_socket_in_updater_sidecar():
    import yaml
    for name in ('docker-compose.yml', 'docker-compose.two-container.yml', 'docker-compose.three-container.yml'):
        data = yaml.safe_load((ROOT / name).read_text(encoding='utf-8'))
        webui = data['services']['hermes-webui']
        updater = data['services']['hermes-webui-updater']
        assert not any('/var/run/docker.sock' in str(item) for item in webui['volumes'])
        assert any('updater-control' in str(item) for item in webui['volumes'])
        assert any('/var/run/docker.sock' in str(item) for item in updater['volumes'])
        assert any('updater-control' in str(item) for item in updater['volumes'])
        assert updater['network_mode'] == 'none'
        assert updater['read_only'] is True
        assert updater['profiles'] == ['self-update']
        env = [str(item) for item in webui['environment']]
        assert any('HERMES_WEBUI_DOCKER_SELF_UPDATE=${HERMES_WEBUI_DOCKER_SELF_UPDATE:-0}' in item for item in env)
        assert any('HERMES_WEBUI_DOCKER_IMAGE=' in item for item in env)
        updater_env = [str(item) for item in updater['environment']]
        assert any('HERMES_WEBUI_DOCKER_SELF_UPDATE=${HERMES_WEBUI_DOCKER_SELF_UPDATE:-0}' in item for item in updater_env)
