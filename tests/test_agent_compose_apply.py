"""Compose Agent image transaction must restore service, not original container ID."""
from pathlib import Path
import copy
import pytest
import yaml
from scripts import agent_compose_transaction as pin
from scripts import agent_compose_apply as apply

OLD = '24802117/hermes-webui@sha256:' + 'a'*64
NEW = '24802117/hermes-webui@sha256:' + 'b'*64
OID = 'c'*64
NID = 'd'*64
RID = 'e'*64

def _fixture(tmp_path):
    p=tmp_path/'compose.yaml'
    p.write_text('services:\n  webui:\n    image: '+OLD+'\n  updater:\n    image: '+OLD+'\n')
    p.chmod(0o600)
    return p

class Harness:
    def __init__(self,p):
        self.p=p; self.owner=OID; self.image='sha256:'+'a'*64
        self.images={OLD:'sha256:'+'a'*64,NEW:'sha256:'+'b'*64}
        self.events=[]; self.fail=False; self.foreign=False
        self.sibling='f'*64
        self.label_hash='1'*64
    def render(self,path): return yaml.safe_load(Path(path).read_text())
    def hash(self): return ('1'*64 if self.p.read_text().find(NEW)<0 else '2'*64)
    def inspect(self):
        return {'Id':self.owner,'Image':self.image,'State':{'Running':True,'Health':{'Status':'healthy'}},
                'Config':{'Labels':{'com.docker.compose.project':'qa','com.docker.compose.service':'webui',
                  'com.docker.compose.project.working_dir':str(self.p.parent),
                  'com.docker.compose.project.config_files':str(self.p),
                  'com.docker.compose.config-hash':self.label_hash}}}
    def image_info(self,ref):return {'Id':self.images[ref],'RepoDigests':[ref]}
    def up(self):
        self.events.append('up')
        if self.foreign:
            self.owner='9'*64;self.image='sha256:'+'9'*64
            raise RuntimeError('foreign takeover')
        self.owner=NID if self.p.read_text().find(NEW)>=0 else RID
        self.image=self.images[NEW if self.owner==NID else OLD]
        self.label_hash=self.hash()
    def accept(self,owner):
        self.events.append('accept')
        if self.fail:raise RuntimeError('injected acceptance failure')
        assert owner['Id']==NID
    def accept_restored(self,owner):
        self.events.append('restore_health')
        assert owner['Image']==self.images[OLD]
    def siblings(self):return self.sibling


def test_success_pins_compose_and_preserves_sibling(tmp_path):
    p=_fixture(tmp_path);h=Harness(p)
    result=apply.apply_compose_agent_image(p,[p],'webui',OLD,NEW,
        repository='24802117/hermes-webui', expected_old_id=OID,
        expected_new_image_id=h.images[NEW], runtime=h)
    assert result['state']=='succeeded'
    assert result['old_id']==OID and result['new_id']==NID
    assert h.render(p)['services']['webui']['image']==NEW
    assert h.siblings()=='f'*64


def test_acceptance_failure_recreates_old_service_and_original_bytes(tmp_path, caplog):
    p=_fixture(tmp_path);initial=p.read_bytes();h=Harness(p);h.fail=True
    result=apply.apply_compose_agent_image(p,[p],'webui',OLD,NEW,
        repository='24802117/hermes-webui',expected_old_id=OID,
        expected_new_image_id=h.images[NEW],runtime=h)
    assert result['state']=='rolled_back'
    assert result['restored_id']==RID and result['restored_id']!=OID
    assert p.read_bytes()==initial and h.events==['up','accept','up','restore_health']
    assert 'injected acceptance failure' in caplog.text


def test_changed_owner_refuses_before_write_or_recreate(tmp_path):
    p=_fixture(tmp_path);initial=p.read_bytes();h=Harness(p)
    with pytest.raises(pin.PinError):
        apply.apply_compose_agent_image(p,[p],'webui',OLD,NEW,
            repository='24802117/hermes-webui',expected_old_id='0'*64,
            expected_new_image_id=h.images[NEW],runtime=h)
    assert p.read_bytes()==initial and not h.events


@pytest.mark.parametrize('fail_on', [1,2])
def test_active_work_before_pin_or_recreate_preserves_old_service(tmp_path, fail_on):
    p=_fixture(tmp_path);h=Harness(p);initial=p.read_bytes();calls=[]
    def guard():
        calls.append('guard')
        if len(calls)==fail_on:raise pin.PinError('active work blocks update')
    h.guard_idle=guard
    if fail_on==1:
        with pytest.raises(pin.PinError,match='active work'):
            apply.apply_compose_agent_image(p,[p],'webui',OLD,NEW,
                repository='24802117/hermes-webui',expected_old_id=OID,
                expected_new_image_id=h.images[NEW],runtime=h)
    else:
        result=apply.apply_compose_agent_image(p,[p],'webui',OLD,NEW,
            repository='24802117/hermes-webui',expected_old_id=OID,
            expected_new_image_id=h.images[NEW],runtime=h)
        assert result['state']=='rolled_back'
    assert p.read_bytes()==initial and h.owner==OID and not h.events


def test_foreign_owner_after_recreate_is_not_overwritten(tmp_path):
    p=_fixture(tmp_path);h=Harness(p);h.foreign=True
    with pytest.raises(pin.PinError,match='foreign'):
        apply.apply_compose_agent_image(p,[p],'webui',OLD,NEW,
            repository='24802117/hermes-webui',expected_old_id=OID,
            expected_new_image_id=h.images[NEW],runtime=h)
    assert h.owner=='9'*64
    # No blind rollback or second compose up over foreign owner.
    assert h.events==['up']


def test_up_failure_before_recreation_restores_only_yaml(tmp_path):
    p=_fixture(tmp_path);initial=p.read_bytes();h=Harness(p)
    def refuse():
        h.events.append('up')
        raise RuntimeError('compose exited before any recreate')
    h.up=refuse
    result=apply.apply_compose_agent_image(p,[p],'webui',OLD,NEW,
        repository='24802117/hermes-webui',expected_old_id=OID,
        expected_new_image_id=h.images[NEW],runtime=h)
    assert result['state']=='rolled_back' and result['restored_id']==OID
    assert p.read_bytes()==initial and h.events==['up']


def test_ambiguous_recreate_keeps_candidate_for_manual_reconciliation(tmp_path):
    p=_fixture(tmp_path);h=Harness(p)
    def ambiguous():
        h.events.append('up')
        h.owner=NID;h.image=h.images[NEW]
        raise RuntimeError('transport lost after recreate')
    h.up=ambiguous
    with pytest.raises(pin.PinError,match='ambiguous'):
        apply.apply_compose_agent_image(p,[p],'webui',OLD,NEW,
            repository='24802117/hermes-webui',expected_old_id=OID,
            expected_new_image_id=h.images[NEW],runtime=h)
    assert h.events==['up'] and h.owner==NID


def test_yaml_verification_failure_after_write_restores_old_bytes(tmp_path, monkeypatch):
    p=_fixture(tmp_path); initial=p.read_bytes(); h=Harness(p)
    def fail_once(plan,render):
        raise RuntimeError('candidate render failed after atomic write')
    monkeypatch.setattr(apply,'verify_pin',fail_once)
    with pytest.raises(pin.PinError,match='candidate'):
        apply.apply_compose_agent_image(p,[p],'webui',OLD,NEW,
            repository='24802117/hermes-webui',expected_old_id=OID,
            expected_new_image_id=h.images[NEW],runtime=h)
    assert p.read_bytes()==initial and h.events==[]


def test_failed_restored_health_does_not_report_rolled_back(tmp_path):
    p=_fixture(tmp_path);h=Harness(p);h.fail=True
    def sick_restoration(owner):
        raise RuntimeError('old image unhealthy')
    h.accept_restored=sick_restoration
    with pytest.raises(pin.PinError,match='restored'):
        apply.apply_compose_agent_image(p,[p],'webui',OLD,NEW,
            repository='24802117/hermes-webui',expected_old_id=OID,
            expected_new_image_id=h.images[NEW],runtime=h)
    assert h.owner==RID and h.image==h.images[OLD]


def test_candidate_owner_changing_during_acceptance_is_not_success(tmp_path):
    p=_fixture(tmp_path);h=Harness(p)
    def stolen(_owner):
        h.events.append('accept')
        h.owner='9'*64;h.image='sha256:'+'9'*64
    h.accept=stolen
    with pytest.raises(pin.PinError,match='foreign'):
        apply.apply_compose_agent_image(p,[p],'webui',OLD,NEW,
            repository='24802117/hermes-webui',expected_old_id=OID,
            expected_new_image_id=h.images[NEW],runtime=h)
    assert h.events==['up','accept'] and h.owner=='9'*64


def test_candidate_compose_hash_changing_during_acceptance_is_not_success(tmp_path):
    p=_fixture(tmp_path);h=Harness(p)
    def changed(_owner):
        h.events.append('accept')
        h.label_hash='9'*64
    h.accept=changed
    h.hash=lambda:'2'*64
    with pytest.raises(pin.PinError):
        apply.apply_compose_agent_image(p,[p],'webui',OLD,NEW,
            repository='24802117/hermes-webui',expected_old_id=OID,
            expected_new_image_id=h.images[NEW],runtime=h)


def test_existing_old_image_must_not_be_pulled_or_retargeted(tmp_path):
    p=_fixture(tmp_path);h=Harness(p)
    h.images[OLD]='sha256:'+'9'*64
    with pytest.raises(pin.PinError):
        apply.apply_compose_agent_image(p,[p],'webui',OLD,NEW,
            repository='24802117/hermes-webui',expected_old_id=OID,
            expected_new_image_id=h.images[NEW],runtime=h)
    assert h.events==[] and p.read_text().find(NEW)<0


def test_transaction_refuses_wrong_final_compose_file_before_writes(tmp_path):
    p=_fixture(tmp_path);other=tmp_path/'override.yaml';other.write_text('services: {}\n')
    h=Harness(p); initial=p.read_bytes()
    h.files=[p,other]
    with pytest.raises(pin.PinError,match='last'):
        apply.apply_compose_agent_image(p,[p,other],'webui',OLD,NEW,
            repository='24802117/hermes-webui',expected_old_id=OID,
            expected_new_image_id=h.images[NEW],runtime=h)
    assert p.read_bytes()==initial and not h.events
