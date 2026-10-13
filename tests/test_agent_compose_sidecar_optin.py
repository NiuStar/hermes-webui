"""Compose opt-in sidecar preflight must be truly install-ready, not a label."""
import base64
import hashlib
import json
import threading
from pathlib import Path
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from api import docker_self_update as dsu
from scripts.agent_compose_runtime import DockerComposeRuntime
from tests.test_agent_unsigned_release import release as unsigned_release


def configured(tmp_path,monkeypatch):
    key=Ed25519PrivateKey.generate()
    public=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    manifest={'schema':'hermes-webui-agent-image/v1','repository':'24802117/hermes-webui',
      'digest':'sha256:'+'a'*64,'platform':'linux/amd64','webui_commit':'b'*40,
      'agent_commit':'c'*40,'webui_version':'v2026.10.08-r2','agent_version':'v0.21.7',
      'verification':{'suite':'agent-image-compatibility','passed':True,'receipt_sha256':'d'*64}}
    raw=json.dumps(manifest,sort_keys=True).encode()
    (tmp_path/'manifest.json').write_bytes(raw)
    (tmp_path/'manifest.json.sig').write_text(base64.b64encode(key.sign(raw)).decode())
    (tmp_path/'publisher.pub').write_text(base64.b64encode(public).decode())
    old=manifest['repository']+'@sha256:'+'f'*64
    p=tmp_path/'compose.yaml';p.write_text('name: qa\nservices:\n  webui:\n    image: '+old+'\n');p.chmod(0o600)
    env={'HERMES_WEBUI_DOCKER_SELF_UPDATE':'1',
      'HERMES_WEBUI_AGENT_MANIFEST_PATH':str(tmp_path/'manifest.json'),
      'HERMES_WEBUI_AGENT_MANIFEST_SIGNATURE_PATH':str(tmp_path/'manifest.json.sig'),
      'HERMES_WEBUI_AGENT_MANIFEST_PUBKEY':str(tmp_path/'publisher.pub'),
      'HERMES_WEBUI_AGENT_MANIFEST_PUBKEY_SHA256':hashlib.sha256(public).hexdigest(),
      'HERMES_WEBUI_AGENT_COMPOSE_ENABLED':'1',
      'HERMES_WEBUI_AGENT_COMPOSE_FILE':str(p),
      'HERMES_WEBUI_AGENT_COMPOSE_PROJECT':'qa',
      'HERMES_WEBUI_AGENT_COMPOSE_SERVICE':'webui',
      'HERMES_WEBUI_UPDATE_TARGET':'hermes-webui'}
    for k,v in env.items():monkeypatch.setenv(k,v)
    monkeypatch.delenv('HERMES_WEBUI_AGENT_COMMIT',raising=False)
    return manifest,old,p


def test_verified_compose_preflight_is_install_ready(tmp_path,monkeypatch):
    manifest,old,p=configured(tmp_path,monkeypatch)
    class Engine:
        def inspect(self,name):
            assert name=='hermes-webui'
            return {'Name':'/hermes-webui','Id':'e'*64,'Image':'sha256:'+'f'*64,
              'State':{'Running':True},'HostConfig':{'Binds':[]},'Mounts':[],
              'Config':{'Env':['HERMES_WEBUI_AGENT_DIR=/opt/hermes'],
              'Labels':{'com.docker.compose.project':'qa','com.docker.compose.service':'webui',
                'com.docker.compose.project.working_dir':str(tmp_path),
                'com.docker.compose.project.config_files':str(p),
                'com.docker.compose.config-hash':'d'*64}}}
        def inspect_image(self,ref):
            if ref==manifest['repository']+'@'+manifest['digest']:
                return {'Id':'sha256:'+'a'*64,'RepoDigests':[ref],
                  'Os':'linux','Architecture':'amd64','Config':{'Labels':{
                  'org.opencontainers.image.version':manifest['webui_version'],
                  'org.opencontainers.image.revision':manifest['webui_commit'],
                  'org.opencontainers.image.hermes-agent.path':'/opt/hermes',
                  'org.opencontainers.image.hermes-agent.revision':manifest['agent_commit'],
                  'org.opencontainers.image.hermes-agent.version':manifest['agent_version']}}}
            return {'Id':'sha256:'+'f'*64,'RepoDigests':[old],
              'Config':{'Labels':{'org.opencontainers.image.version':manifest['webui_version'],
              'org.opencontainers.image.revision':manifest['webui_commit'],
              'org.opencontainers.image.hermes-agent.path':'/opt/hermes',
              'org.opencontainers.image.hermes-agent.revision':'d'*40}}}
        def pull(self,*_):pytest.fail('preflight cannot pull')
    monkeypatch.setattr(dsu,'DockerEngine',Engine)
    monkeypatch.setattr(DockerComposeRuntime,'render',lambda self,path:{'name':'qa','services':{'webui':{'image':old}}})
    monkeypatch.setattr(DockerComposeRuntime,'hash',lambda self:'d'*64)
    monkeypatch.setattr(DockerComposeRuntime,'siblings',lambda self:())
    response,worker=dsu._control_request({'action':'agent_preflight','token':'secret'},
                                          busy=threading.Lock(),expected_token='secret')
    assert response['ok'] is True and response['latest_sha']==manifest['agent_commit']
    assert worker is None


def test_unsigned_compose_preflight_is_install_ready(tmp_path,monkeypatch):
    manifest,old,p=configured(tmp_path,monkeypatch)
    pin=unsigned_release(tmp_path)
    pin_data=json.loads(pin.read_text())
    pin_data.update({'webui_commit':manifest['webui_commit'],
                     'webui_version':manifest['webui_version'],
                     'agent_commit':manifest['agent_commit'],
                     'agent_version':manifest['agent_version']})
    pin.write_text(json.dumps(pin_data))
    for name in ('HERMES_WEBUI_AGENT_MANIFEST_PATH',
                 'HERMES_WEBUI_AGENT_MANIFEST_SIGNATURE_PATH',
                 'HERMES_WEBUI_AGENT_MANIFEST_PUBKEY',
                 'HERMES_WEBUI_AGENT_MANIFEST_PUBKEY_SHA256'):
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv('HERMES_WEBUI_AGENT_RELEASE_FILE',str(pin))
    class Engine:
        def inspect(self,name):
            assert name=='hermes-webui'
            return {'Name':'/hermes-webui','Id':'e'*64,'Image':'sha256:'+'f'*64,
              'State':{'Running':True},'HostConfig':{'Binds':[]},'Mounts':[],
              'Config':{'Env':['HERMES_WEBUI_AGENT_DIR=/opt/hermes'],
              'Labels':{'com.docker.compose.project':'qa','com.docker.compose.service':'webui',
                'com.docker.compose.project.working_dir':str(tmp_path),
                'com.docker.compose.project.config_files':str(p),
                'com.docker.compose.config-hash':'d'*64}}}
        def inspect_image(self,ref):
            if ref==manifest['repository']+'@'+manifest['digest']:
                return {'Id':'sha256:'+'a'*64,'RepoDigests':[ref],
                  'Os':'linux','Architecture':'amd64','Config':{'Labels':{
                  'org.opencontainers.image.version':manifest['webui_version'],
                  'org.opencontainers.image.revision':manifest['webui_commit'],
                  'org.opencontainers.image.hermes-agent.path':'/opt/hermes',
                  'org.opencontainers.image.hermes-agent.revision':manifest['agent_commit'],
                  'org.opencontainers.image.hermes-agent.version':manifest['agent_version']}}}
            return {'Id':'sha256:'+'f'*64,'RepoDigests':[old],
              'Config':{'Labels':{'org.opencontainers.image.version':manifest['webui_version'],
              'org.opencontainers.image.revision':manifest['webui_commit'],
              'org.opencontainers.image.hermes-agent.path':'/opt/hermes',
              'org.opencontainers.image.hermes-agent.revision':'d'*40}}}
        def pull(self,*_):pytest.fail('preflight cannot pull')
    monkeypatch.setattr(dsu,'DockerEngine',Engine)
    monkeypatch.setattr(DockerComposeRuntime,'render',lambda self,path:{'name':'qa','services':{'webui':{'image':old}}})
    monkeypatch.setattr(DockerComposeRuntime,'hash',lambda self:'d'*64)
    monkeypatch.setattr(DockerComposeRuntime,'siblings',lambda self:())
    response,worker=dsu._control_request({'action':'agent_preflight','token':'secret'},
        busy=threading.Lock(),expected_token='secret')
    assert response['ok'] is True and response['latest_sha']==manifest['agent_commit']
    assert worker is None


def test_missing_compose_opt_in_cannot_use_signed_candidate(tmp_path,monkeypatch):
    manifest,old,p=configured(tmp_path,monkeypatch)
    monkeypatch.delenv('HERMES_WEBUI_AGENT_COMPOSE_FILE')
    result,worker=dsu._control_request({'action':'agent_preflight','token':'secret'},
                                       busy=threading.Lock(),expected_token='secret')
    assert result['ok'] is False and worker is None


def test_compose_preflight_rejects_signed_image_absent_before_docker_writes(tmp_path,monkeypatch):
    manifest,old,p=configured(tmp_path,monkeypatch)
    class Engine:
        def inspect(self,name):
            return {'Name':'/hermes-webui','Id':'e'*64,'Image':'sha256:'+'f'*64,
                'State':{'Running':True},'HostConfig':{'Binds':[]},'Mounts':[],
                'Config':{'Env':['HERMES_WEBUI_AGENT_DIR=/opt/hermes'],
                'Labels':{'com.docker.compose.project':'qa','com.docker.compose.service':'webui',
                    'com.docker.compose.project.working_dir':str(tmp_path),
                    'com.docker.compose.project.config_files':str(p),
                    'com.docker.compose.config-hash':'d'*64}}}
        def inspect_image(self,ref):
            if ref==manifest['repository']+'@'+manifest['digest']:
                raise dsu.DockerEngineError('Docker API 404: signed image absent')
            return {'Id':'sha256:'+'f'*64,'RepoDigests':[old],
                'Config':{'Labels':{'org.opencontainers.image.version':manifest['webui_version'],
                    'org.opencontainers.image.revision':manifest['webui_commit'],
                    'org.opencontainers.image.hermes-agent.path':'/opt/hermes',
                    'org.opencontainers.image.hermes-agent.revision':'d'*40}}}
        def pull(self,*_):pytest.fail('preflight may not pull')
    monkeypatch.setattr(dsu,'DockerEngine',Engine)
    monkeypatch.setattr(DockerComposeRuntime,'render',lambda self,path:{'name':'qa','services':{'webui':{'image':old}}})
    monkeypatch.setattr(DockerComposeRuntime,'hash',lambda self:'d'*64)
    monkeypatch.setattr(DockerComposeRuntime,'siblings',lambda self:())
    result,worker=dsu._control_request({'action':'agent_preflight','token':'secret'},
                                       busy=threading.Lock(),expected_token='secret')
    assert result['ok'] is False and worker is None


def test_compose_preflight_rejects_drifted_owner_config_before_docker_writes(tmp_path,monkeypatch):
    manifest,old,p=configured(tmp_path,monkeypatch)
    class Engine:
        def inspect(self,name):
            return {'Name':'/hermes-webui','Id':'e'*64,'Image':'sha256:'+'f'*64,
                'State':{'Running':True},'HostConfig':{'Binds':[]},'Mounts':[],
                'Config':{'Env':['HERMES_WEBUI_AGENT_DIR=/opt/hermes'],
                'Labels':{'com.docker.compose.project':'qa','com.docker.compose.service':'webui',
                    'com.docker.compose.project.working_dir':str(tmp_path),
                    'com.docker.compose.project.config_files':str(tmp_path/'different.yaml'),
                    'com.docker.compose.config-hash':'d'*64}}}
        def inspect_image(self,ref):
            if ref==manifest['repository']+'@'+manifest['digest']:
                pytest.fail('mismatched config must reject before target image')
            return {'Id':'sha256:'+'f'*64,'RepoDigests':[old],
                'Config':{'Labels':{'org.opencontainers.image.version':manifest['webui_version'],
                    'org.opencontainers.image.revision':manifest['webui_commit'],
                    'org.opencontainers.image.hermes-agent.path':'/opt/hermes',
                    'org.opencontainers.image.hermes-agent.revision':'d'*40}}}
        def pull(self,*_):pytest.fail('preflight may not pull')
    monkeypatch.setattr(dsu,'DockerEngine',Engine)
    monkeypatch.setattr(DockerComposeRuntime,'render',lambda self,path:{'name':'qa','services':{'webui':{'image':old}}})
    monkeypatch.setattr(DockerComposeRuntime,'hash',lambda self:'d'*64)
    result,worker=dsu._control_request({'action':'agent_preflight','token':'secret'},
                                       busy=threading.Lock(),expected_token='secret')
    assert result['ok'] is False and worker is None


def test_compose_update_worker_never_uses_unmanaged_replacer(monkeypatch):
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_SELF_UPDATE','1')
    monkeypatch.setenv('HERMES_WEBUI_AGENT_MANIFEST_PATH','/qa/manifest')
    monkeypatch.setenv('HERMES_WEBUI_AGENT_MANIFEST_SIGNATURE_PATH','/qa/signature')
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_IMAGE','24802117/hermes-webui')
    expected={'image':'24802117/hermes-webui@sha256:'+'a'*64,
              'agent_commit':'c'*40,'agent_version':'v0.21.7',
              'webui_version':'v2026.10.08-r2','webui_commit':'b'*40,
              'platform':'linux/amd64'}
    old_image={'Id':'sha256:'+'f'*64,'Config':{'Labels':{
      'org.opencontainers.image.version':expected['webui_version'],
      'org.opencontainers.image.revision':expected['webui_commit'],
      'org.opencontainers.image.hermes-agent.path':'/opt/hermes',
      'org.opencontainers.image.hermes-agent.revision':'d'*40}}}
    live={'Id':'e'*64,'Image':old_image['Id'],'State':{'Running':True},
          'Config':{'Labels':{'com.docker.compose.project':'qa'},
                    'Env':['HERMES_WEBUI_AGENT_DIR=/opt/hermes']},
          'HostConfig':{'Binds':[]},'Mounts':[]}
    class Engine:
        def inspect(self,_):return live
        def inspect_image(self,_):return old_image
    monkeypatch.setattr(dsu,'DockerEngine',Engine)
    monkeypatch.setattr(dsu,'preflight_agent_manifest',lambda *_a,**_k:expected)
    class Runtime:
        files=[Path('/qa/compose.yaml')]
        service='webui'
        repository='24802117/hermes-webui'
        def image_info(self,_):return {'Id':'sha256:'+'a'*64}
    runtime=Runtime()
    monkeypatch.setattr(dsu,'_compose_agent_preflight',lambda *_:(runtime,'24802117/hermes-webui@sha256:'+'f'*64))
    monkeypatch.setattr(dsu,'replace_container',lambda *_a,**_k:pytest.fail('old replacer forbidden'))
    calls=[]
    monkeypatch.setattr(dsu,'_apply_signed_compose_agent',lambda *args,**kw:(calls.append((args,kw)) or {'state':'succeeded','new_id':'1'*64}))
    busy=threading.Lock(); progress=dsu.UpdateProgress()
    result,worker=dsu._control_request({'action':'update_agent','token':'secret'},busy=busy,
                                       expected_token='secret',progress=progress)
    assert result['ok'] and worker is not None
    worker.start();worker.join(timeout=5)
    assert not worker.is_alive() and len(calls)==1
    assert progress.snapshot(result['operation_id'])['state']=='succeeded'


def test_compose_rollback_is_reported_as_failed_operation(monkeypatch):
    # Run the same signed opt-in setup but force the transaction's restored-old result.
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_SELF_UPDATE','1')
    monkeypatch.setenv('HERMES_WEBUI_AGENT_MANIFEST_PATH','/qa/manifest')
    monkeypatch.setenv('HERMES_WEBUI_AGENT_MANIFEST_SIGNATURE_PATH','/qa/signature')
    monkeypatch.setenv('HERMES_WEBUI_DOCKER_IMAGE','24802117/hermes-webui')
    expected={'image':'24802117/hermes-webui@sha256:'+'a'*64,
              'agent_commit':'c'*40,'agent_version':'v0.21.7',
              'webui_version':'v2026.10.08-r2','webui_commit':'b'*40,'platform':'linux/amd64'}
    class Engine:
        def inspect(self,_):return {'Id':'e'*64,'Image':'sha256:'+'f'*64,'State':{'Running':True},
            'Config':{'Labels':{'com.docker.compose.project':'qa'},'Env':['HERMES_WEBUI_AGENT_DIR=/opt/hermes']},
            'HostConfig':{'Binds':[]},'Mounts':[]}
        def inspect_image(self,_):return {'Id':'sha256:'+'f'*64,'Config':{'Labels':{
            'org.opencontainers.image.version':expected['webui_version'],
            'org.opencontainers.image.revision':expected['webui_commit'],
            'org.opencontainers.image.hermes-agent.path':'/opt/hermes',
            'org.opencontainers.image.hermes-agent.revision':'d'*40}}}
    monkeypatch.setattr(dsu,'DockerEngine',Engine)
    monkeypatch.setattr(dsu,'preflight_agent_manifest',lambda *_a,**_k:expected)
    monkeypatch.setattr(dsu,'_compose_agent_preflight',lambda *_:(object(),'24802117/hermes-webui@sha256:'+'f'*64))
    monkeypatch.setattr(dsu,'_apply_signed_compose_agent',lambda *_a,**_k:{'state':'rolled_back','restored_id':'2'*64})
    progress=dsu.UpdateProgress();busy=threading.Lock()
    result,worker=dsu._control_request({'action':'update_agent','token':'secret'},busy=busy,
                                       expected_token='secret',progress=progress)
    assert result['ok']
    worker.start();worker.join(timeout=5)
    assert not worker.is_alive()
    assert progress.snapshot(result['operation_id'])['state']=='failed'
