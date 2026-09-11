"""Runner-only tests; neither synthetic evidence nor success admits a volume."""
import importlib.util
import pytest


PROFILE_ID = '1' * 32


def profile():
    return dict(format_version=1, hard_limit_profile_id=PROFILE_ID,
                unit='hermes-bootstrap-test.service', creator_uid=999, creator_gid=987,
                memory_max_bytes=67108864, memory_swap_max_bytes=0,
                pids_max=16, runtime_max_usec=60000000,
                mount_namespace='mnt:[1]', cgroup_namespace='cgroup:[2]', user_namespace='user:[3]')


@pytest.mark.parametrize('key,value', [
    ('format_version', True), ('creator_uid', 0), ('creator_gid', False),
    ('memory_max_bytes', '67108864'), ('memory_swap_max_bytes', 1),
    ('pids_max', 0), ('runtime_max_usec', -1), ('unit', 'ssh.service'),
    ('unit', 'hermes-bootstrap-../x.service'), ('hard_limit_profile_id', '2' * 32),
    ('extra', True),
])
def test_strict_profile(key, value):
    from api.display_bootstrap_runner import _validate_profile
    from api.display_bootstrap_policy import BootstrapRejected
    data = profile()
    data[key] = value
    with pytest.raises(BootstrapRejected, match='INVALID_INPUT'):
        _validate_profile(data, PROFILE_ID)


def test_live_verifier_exists():
    from api import display_bootstrap_runner as runner
    assert callable(getattr(runner, 'verify_systemd_runner', None))


@pytest.mark.parametrize('field,value', [
    ('KillSignal', '15'), ('FinalKillSignal', '15'), ('Restart', 'always'),
])
def test_worker_rejects_graceful_kill_or_restart(monkeypatch, field, value):
    from api import display_bootstrap_runner as runner
    from api.display_bootstrap_policy import BootstrapRejected
    p = profile()
    monkeypatch.setattr(runner, 'read_hard_limit_profile', lambda _: (p, 'a'*64))
    monkeypatch.setattr(runner, 'verify_process_identity', lambda _: None)
    monkeypatch.setattr(runner.os, 'getresgid', lambda: (987, 987, 987))
    relative = '/system.slice/' + p['unit']
    monkeypatch.setattr(runner, '_self_cgroup', lambda: relative)
    values = dict(Id=p['unit'], MainPID=str(runner.os.getpid()), ControlGroup=relative,
                  LoadState='loaded', ActiveState='active', SubState='running',
                  Delegate='no', KillMode='control-group', SendSIGKILL='yes',
                  NoNewPrivileges='yes', User='999', Group='987', Type='exec',
                  KillSignal='9', FinalKillSignal='9', Restart='no',
                  RuntimeMaxUSec=p['runtime_max_usec'])
    values[field] = value
    monkeypatch.setattr(runner, '_systemd_properties', lambda _: values)
    def forbidden(*args):
        pytest.fail('invalid unit reached cgroup admission')
    monkeypatch.setattr(runner, '_check_cgroup', forbidden)
    with pytest.raises(BootstrapRejected, match='ACCESS_BOUNDARY_UNPROVEN'):
        runner.verify_systemd_runner(PROFILE_ID, max_rss_bytes=p['memory_max_bytes'],
                                     max_elapsed_seconds=60)


def test_runner_module_exists():
    assert importlib.util.find_spec('api.display_bootstrap_runner') is not None


@pytest.mark.parametrize('value', [None, True, {}, '', '../x', 'A' * 32, '0' * 31])
def test_profile_id_rejected_before_io(value):
    from api.display_bootstrap_runner import read_hard_limit_profile
    from api.display_bootstrap_policy import BootstrapRejected
    with pytest.raises(BootstrapRejected, match='INVALID_INPUT'):
        read_hard_limit_profile(value)
