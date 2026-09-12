from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT = (ROOT / 'docker_init.bash').read_text(encoding='utf-8')


def test_webui_init_does_not_grant_docker_socket_access():
    assert 'hostdocker' not in INIT
    assert "stat -c '%g' /var/run/docker.sock" not in INIT


def test_uid_remap_does_not_traverse_read_only_home_mounts():
    assert 'passwd = Path("/etc/passwd")' in INIT
    assert 'fields[2] = str(wanted_uid)' in INIT
    assert 'wanted UID {wanted_uid} is already assigned to {fields[0]}' in INIT
    assert 'os.replace(tmp_name, passwd)' in INIT
    assert 'usermod -o -u "${WANTED_UID}" hermeswebui' not in INIT


def test_home_chown_ignores_only_entries_that_vanish_during_walk():
    assert 'os.walk(home, topdown=True, followlinks=False, onerror=walk_error)' in INIT
    assert 'except FileNotFoundError:' in INIT
    assert 'os.lchown(path, wanted_uid, wanted_gid)' in INIT
    assert '-exec chown -h "${WANTED_UID}:${WANTED_GID}" {} +' not in INIT


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
        healthcheck = updater['healthcheck']
        assert healthcheck['test'] == [
            'CMD', '/usr/local/bin/python', '/apptoo/api/docker_self_update.py',
            '--healthcheck', '/run/hermes-webui-updater/control.sock',
        ]
        assert healthcheck['interval'] == '10s'
        assert healthcheck['timeout'] == '5s'
        assert healthcheck['retries'] == 6
        assert updater['profiles'] == ['self-update']
        env = [str(item) for item in webui['environment']]
        assert any('HERMES_WEBUI_DOCKER_SELF_UPDATE=${HERMES_WEBUI_DOCKER_SELF_UPDATE:-0}' in item for item in env)
        assert any('HERMES_WEBUI_DOCKER_IMAGE=' in item for item in env)
        updater_env = [str(item) for item in updater['environment']]
        assert any('HERMES_WEBUI_DOCKER_SELF_UPDATE=${HERMES_WEBUI_DOCKER_SELF_UPDATE:-0}' in item for item in updater_env)
