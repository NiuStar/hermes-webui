"""Signed Agent image identity must be checked before stopping an old container."""
import pytest

from api import docker_self_update as dsu


def _image():
    return {
        'Id': 'sha256:' + 'f' * 64,
        'RepoDigests': ['24802117/hermes-webui@sha256:' + 'a' * 64],
        'Os': 'linux', 'Architecture': 'amd64',
        'Config': {'Labels': {
            'org.opencontainers.image.version': 'v2026.10.08-r2',
            'org.opencontainers.image.revision': 'b' * 40,
            'org.opencontainers.image.hermes-agent.revision': 'c' * 40,
            'org.opencontainers.image.hermes-agent.version': 'v0.21.6',
            'org.opencontainers.image.hermes-agent.path': '/opt/hermes',
        }},
    }


def _signed():
    return {'image': '24802117/hermes-webui@sha256:' + 'a' * 64,
            'platform': 'linux/amd64', 'webui_commit': 'b' * 40,
            'agent_commit': 'c' * 40, 'webui_version': 'v2026.10.08-r2',
            'agent_version': 'v0.21.6'}


def test_direct_signed_replacement_refuses_compose_owner_before_pull(monkeypatch):
    old = {'Name': '/hermes-webui', 'Id': 'a' * 64, 'State': {'Running': True},
           'Config': {'Labels': {'com.docker.compose.project': 'qa'}},
           'HostConfig': {'Binds': []}, 'Mounts': []}

    class Engine:
        def inspect(self, target):
            assert target == 'hermes-webui'
            return old
        def pull(self, *_): pytest.fail('Compose target must not pull')

    monkeypatch.setattr(dsu, 'DockerEngine', Engine)
    with pytest.raises(dsu.DockerEngineError, match='Compose'):
        dsu.replace_container('hermes-webui', _signed()['image'],
                              expected_agent_image=_signed())


def test_signed_replacement_refuses_changed_preflight_owner_before_pull(monkeypatch):
    old = {'Name': '/hermes-webui', 'Id': 'b' * 64, 'State': {'Running': True},
           'Config': {'Labels': {}}, 'HostConfig': {'Binds': []}, 'Mounts': []}

    class Engine:
        def inspect(self, target):
            assert target == 'hermes-webui'
            return old
        def pull(self, *_): pytest.fail('changed owner must not pull')

    monkeypatch.setattr(dsu, 'DockerEngine', Engine)
    with pytest.raises(dsu.DockerEngineError, match='owner'):
        dsu.replace_container('hermes-webui', _signed()['image'],
                              expected_agent_image=_signed(), expected_old_id='a' * 64)


def test_signed_replacement_refuses_takeover_during_pull_before_stop(monkeypatch):
    old = {'Name': '/hermes-webui', 'Id': 'a' * 64, 'Image': 'sha256:' + 'e' * 64,
           'State': {'Running': True},
           'Config': {'Labels': {}, 'Env': ['HERMES_WEBUI_AGENT_DIR=/opt/hermes']},
           'HostConfig': {'Binds': []}, 'Mounts': []}

    class Engine:
        changed = False
        def inspect(self, target):
            if target == 'hermes-webui':
                return {**old, 'Id': ('b' * 64 if self.changed else old['Id'])}
            raise dsu.DockerEngineError('Docker API 404')
        def pull(self, *_): self.changed = True
        def inspect_image(self, ref):
            if ref == old['Image']:
                return {**_image(), 'Id': ref}
            return _image()
        def rename(self, *_): pytest.fail('foreign owner must not be renamed')
        def stop(self, *_): pytest.fail('foreign owner must not be stopped')

    monkeypatch.setattr(dsu, 'DockerEngine', Engine)
    with pytest.raises(dsu.DockerEngineError, match='owner'):
        dsu.replace_container('hermes-webui', _signed()['image'],
                              expected_agent_image=_signed(), expected_old_id='a' * 64)


def test_signed_takeover_stops_by_immutable_old_id_not_backup_name(monkeypatch):
    old_id = 'a' * 64
    old = {'Name': '/hermes-webui', 'Id': old_id, 'Image': 'sha256:' + 'e' * 64,
           'State': {'Running': True},
           'Config': {'Labels': {}, 'Env': ['HERMES_WEBUI_AGENT_DIR=/opt/hermes']},
           'HostConfig': {'Binds': []}, 'Mounts': []}
    class Engine:
        def __init__(self): self.renamed = False; self.stops = []
        def inspect(self, name):
            if name == 'hermes-webui.hermes-update-new' or (name == 'hermes-webui.hermes-update-old' and not self.renamed):
                raise dsu.DockerEngineError('Docker API 404: not found')
            if name == 'hermes-webui.hermes-update-old' and self.renamed:
                return {**old, 'Id': 'b' * 64}  # foreign owner took backup name
            return old
        def pull(self, _): pass
        def inspect_image(self, ref):
            if ref == old['Image']:
                return {**_image(), 'Id': ref}
            return _image()
        def rename(self, src, dst):
            self.renamed = True
        def stop(self, identifier):
            self.stops.append(identifier)
            raise dsu.DockerEngineError('injected stop failure')
        def remove(self, *_args, **_kwargs): pytest.fail('must not delete unknown owner')
    engine = Engine()
    monkeypatch.setattr(dsu, 'DockerEngine', lambda: engine)
    with pytest.raises(dsu.DockerEngineError, match='rollback failed'):
        dsu.replace_container('hermes-webui', _signed()['image'], expected_agent_image=_signed(),
                              expected_old_id=old_id, timeout=1)
    assert engine.stops == [old_id]


def test_pulled_image_matches_verified_manifest():
    assert dsu._verified_agent_image_id(_image(), _signed()) == 'sha256:' + 'f' * 64


def test_agent_only_target_must_match_current_webui_release():
    old = _image()
    dsu._require_same_webui_identity(old, _signed())
    for field, value in (('org.opencontainers.image.version', 'v2026.10.09-qa'),
                         ('org.opencontainers.image.revision', 'd' * 40)):
        changed = _image()
        changed['Config']['Labels'][field] = value
        with pytest.raises(dsu.DockerEngineError, match='WebUI release identity'):
            dsu._require_same_webui_identity(changed, _signed())


def test_signed_update_rejects_persistent_app_mount_before_stop(monkeypatch):
    old = {'Name': '/hermes-webui', 'Id': 'a' * 64, 'Image': 'sha256:' + 'e' * 64,
           'State': {'Running': True},
           'Config': {'Env': ['HERMES_WEBUI_AGENT_DIR=/opt/hermes']},
           'HostConfig': {'Binds': ['/state/app:/app:rw']}, 'Mounts': []}
    class Engine:
        def __init__(self): self.calls = []
        def inspect(self, name):
            if name != 'hermes-webui':
                raise dsu.DockerEngineError('not found')
            return old
        def pull(self, image): self.calls.append(('pull', image))
        def inspect_image(self, ref):
            if ref == old['Image']:
                return {**_image(), 'Id': ref}
            return _image()
        def rename(self, *_args): self.calls.append(('rename',))
        def stop(self, *_args): self.calls.append(('stop',))
    engine = Engine()
    monkeypatch.setattr(dsu, 'DockerEngine', lambda: engine)
    with pytest.raises(dsu.DockerEngineError, match='mount'):
        dsu.replace_container('hermes-webui', _signed()['image'], expected_agent_image=_signed())
    assert engine.calls == [('pull', _signed()['image'])]


@pytest.mark.parametrize('reserved', ['hermes-webui.hermes-update-old', 'hermes-webui.hermes-update-new'])
def test_signed_update_rejects_existing_transaction_owner_before_stop(monkeypatch, reserved):
    old = {'Name': '/hermes-webui', 'Id': 'a' * 64, 'Image': 'sha256:' + 'e' * 64,
           'State': {'Running': True}, 'Config': {'Env': ['HERMES_WEBUI_AGENT_DIR=/opt/hermes']},
           'HostConfig': {'Binds': []}, 'Mounts': []}
    class Engine:
        def __init__(self): self.calls = []
        def inspect(self, name):
            if name == reserved:
                return {'Id': 'b' * 64, 'Name': '/' + reserved}
            if name == 'hermes-webui': return old
            raise dsu.DockerEngineError('not found')
        def pull(self, image): self.calls.append(('pull', image))
        def inspect_image(self, name): return _image()
        def rename(self, *_): self.calls.append(('rename',))
        def stop(self, *_): self.calls.append(('stop',))
    engine = Engine()
    monkeypatch.setattr(dsu, 'DockerEngine', lambda: engine)
    monkeypatch.setattr(dsu, '_wait_for_restored_health',
                        lambda *_: (_ for _ in ()).throw(dsu.DockerEngineError('unexpected rollback')))
    with pytest.raises(dsu.DockerEngineError, match='transaction'):
        dsu.replace_container('hermes-webui', _signed()['image'], expected_agent_image=_signed(), timeout=1)
    assert not any(call[0] in ('rename', 'stop') for call in engine.calls)


def test_rename_ack_failure_after_takeover_restores_only_original_owner(monkeypatch):
    old = {'Name': '/hermes-webui', 'Id': 'a' * 64, 'Image': 'sha256:' + 'e' * 64,
           'State': {'Running': True, 'Health': {'Status': 'healthy'}},
           'Config': {'Env': ['HERMES_WEBUI_AGENT_DIR=/opt/hermes']},
           'HostConfig': {'Binds': []}, 'Mounts': []}
    class Engine:
        def __init__(self): self.calls = []; self.renamed = False
        def inspect(self, name):
            if name == 'hermes-webui' and not self.renamed: return old
            if name == 'hermes-webui.hermes-update-old' and self.renamed:
                return {**old, 'Name': '/' + name}
            raise dsu.DockerEngineError('Docker API 404: no such container')
        def pull(self, image): pass
        def inspect_image(self, image): return _image()
        def rename(self, src, dst):
            self.calls.append(('rename', src, dst))
            if src == old['Id'] and dst == 'hermes-webui.hermes-update-old':
                self.renamed = True
                raise dsu.DockerEngineError('Docker transport closed after rename')
            self.renamed = False
        def stop(self, *_): self.calls.append(('stop',))
        def remove(self, *_args, **_kwargs): self.calls.append(('remove',))
    engine = Engine()
    monkeypatch.setattr(dsu, 'DockerEngine', lambda: engine)
    with pytest.raises(dsu.DockerEngineError, match='transport closed'):
        dsu.replace_container('hermes-webui', _signed()['image'], expected_agent_image=_signed())
    assert engine.renamed is False
    assert engine.calls == [('rename', old['Id'], 'hermes-webui.hermes-update-old'),
                            ('rename', old['Id'], 'hermes-webui')]


def test_rollback_never_removes_foreign_name_owner(monkeypatch):
    old = {'Name': '/hermes-webui', 'Id': 'a' * 64, 'Image': 'sha256:' + 'e' * 64,
           'State': {'Running': True, 'Health': {'Status': 'healthy'}},
           'Config': {'Env': ['HERMES_WEBUI_AGENT_DIR=/opt/hermes']},
           'HostConfig': {'Binds': []}, 'Mounts': []}
    class Engine:
        def __init__(self): self.calls = []; self.stopped = False
        def inspect(self, name):
            if name == 'hermes-webui':
                return ({'Id': 'd' * 64, 'State': {'Running': True}}
                        if self.stopped else old)
            if name == 'hermes-webui.hermes-update-old' and self.stopped:
                return {**old, 'Name': '/' + name}
            raise dsu.DockerEngineError('Docker API 404: not found')
        def pull(self, _): pass
        def inspect_image(self, _): return _image()
        def rename(self, src, dst): self.calls.append(('rename', src, dst))
        def stop(self, name):
            self.calls.append(('stop', name)); self.stopped = True
            raise dsu.DockerEngineError('stop transport failed')
        def remove(self, name, *, force=False): self.calls.append(('remove', name))
    engine = Engine()
    monkeypatch.setattr(dsu, 'DockerEngine', lambda: engine)
    with pytest.raises(dsu.DockerEngineError, match='rollback failed'):
        dsu.replace_container('hermes-webui', _signed()['image'], expected_agent_image=_signed(), timeout=1)
    assert not any(call[0] == 'remove' for call in engine.calls)


@pytest.mark.parametrize('mutate', [
    lambda image: image.update(RepoDigests=[]),
    lambda image: image.update(RepoDigests=['24802117/hermes-webui@sha256:' + 'e' * 64]),
    lambda image: image.update(Architecture='arm64'),
    lambda image: image['Config']['Labels'].update({'org.opencontainers.image.revision': 'd' * 40}),
    lambda image: image['Config']['Labels'].update({'org.opencontainers.image.hermes-agent.revision': 'd' * 40}),
    lambda image: image['Config']['Labels'].update({'org.opencontainers.image.hermes-agent.version': 'v0.21.5'}),
    lambda image: image.update(Id='sha256:garbage'),
])
def test_mismatch_rejected(mutate):
    image = _image()
    mutate(image)
    with pytest.raises(dsu.DockerEngineError):
        dsu._verified_agent_image_id(image, _signed())


def test_replace_rejects_unverified_image_before_stop(monkeypatch):
    old = {'Name': '/hermes-webui', 'Id': 'a' * 64, 'State': {'Running': True}}
    class Engine:
        def __init__(self): self.calls = []
        def inspect(self, name):
            if name != 'hermes-webui':
                raise dsu.DockerEngineError('not found')
            return old
        def pull(self, image): self.calls.append(('pull', image))
        def inspect_image(self, image_reference):
            image = _image()
            image['Architecture'] = 'arm64'
            return image
        def rename(self, _old, _new): self.calls.append(('rename',))
        def stop(self, _name): self.calls.append(('stop',))
    engine = Engine()
    monkeypatch.setattr(dsu, 'DockerEngine', lambda: engine)
    with pytest.raises(dsu.DockerEngineError):
        dsu.replace_container('hermes-webui', _signed()['image'],
                              expected_agent_image=_signed())
    assert engine.calls == [('pull', _signed()['image'])]


@pytest.mark.parametrize('change', [
    lambda old: old['Config']['Env'].append('HERMES_WEBUI_AGENT_DIR=/custom/agent'),
    lambda old: old['HostConfig']['Binds'].append('/host/agent:/opt/hermes/hermes_cli:ro'),
    lambda old: old['HostConfig']['Mounts'].append({'Target': '/opt/hermes', 'Type': 'volume'}),
    lambda old: old['Config']['Env'].append('PYTHONPATH=/custom/agent:/opt/hermes'),
    lambda old: old['HostConfig']['Binds'].append('/host/app:/app:rw'),
    lambda old: old['HostConfig']['Binds'].append('/host/venv:/app/venv:rw'),
    lambda old: old['HostConfig']['Mounts'].append({'Target': '/app/cache', 'Type': 'volume'}),
    lambda old: old['Mounts'].append({'Destination': '/app/hermes-agent-src-alt', 'Type': 'bind'}),
    lambda old: old['HostConfig'].update(Tmpfs={'/app/venv': 'rw'}),
    lambda old: old['Config']['Env'].append('PYTHONPATH=/app/hermes-agent-src'),
    lambda old: old['HostConfig']['Mounts'].append({'Target': '/home/hermeswebui/.hermes/hermes-agent', 'Type': 'volume'}),
    lambda old: old['HostConfig'].update(Tmpfs={'/opt/hermes': 'rw'}),
    lambda old: old['HostConfig'].update(Tmpfs={'/opt': 'rw'}),
    lambda old: old['HostConfig'].update(Tmpfs={'/app/hermes-agent-src': 'rw'}),
])
def test_signed_update_refuses_runtime_agent_overrides_before_stop(monkeypatch, change):
    old = {'Name': '/hermes-webui', 'Id': 'a' * 64, 'State': {'Running': True},
           'Config': {'Env': ['HERMES_WEBUI_AGENT_DIR=/opt/hermes', 'PYTHONPATH=/opt/hermes']},
           'HostConfig': {'Binds': [], 'Mounts': []}, 'Mounts': []}
    change(old)
    class Engine:
        def __init__(self): self.calls = []
        def inspect(self, name):
            if name != 'hermes-webui':
                raise dsu.DockerEngineError('not found')
            return old
        def pull(self, image): self.calls.append(('pull', image))
        def inspect_image(self, _image_name): return _image()
        def rename(self, _old, _new): self.calls.append(('rename',))
        def stop(self, _name): self.calls.append(('stop',))
    engine = Engine()
    monkeypatch.setattr(dsu, 'DockerEngine', lambda: engine)
    with pytest.raises(dsu.DockerEngineError):
        dsu.replace_container('hermes-webui', _signed()['image'], expected_agent_image=_signed())
    assert engine.calls == [('pull', _signed()['image'])]
