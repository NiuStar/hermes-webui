"""V3 state-machine harness: real media, mocked OS admission only.

This does not prove kernel, observer, quota or worker-release acceptance.
"""
import os
import secrets
from pathlib import Path
from types import SimpleNamespace
import pytest
from api import display_bootstrap_admission as admission
from api import display_bootstrap_admission_ownership as ownership
from api import display_bootstrap_preparation_steps as steps
from api import display_bootstrap_policy as policy
from api import display_bootstrap_runner_binding as binding
from api.display_bootstrap_manifest import canonical_bytes, digest
from api.display_bootstrap_artifact import PINNED_DDL_SHA
from tests.test_display_bootstrap_v3 import resource


@pytest.fixture
def v3_protocol(monkeypatch):
    import time
    parent = Path(os.environ['LIFECYCLE_TEST_ROOT'])
    audit_parent = Path(os.environ['LIFECYCLE_AUDIT_TEST_ROOT'])
    assert parent.stat().st_dev != audit_parent.stat().st_dev
    token = secrets.token_hex(16)
    base, audit_base = parent/token, audit_parent/token
    base.mkdir(mode=0o700)
    audit_base.mkdir(mode=0o700)
    paths = {}
    for key in ('candidate_root','publish_root','registry_root','approval_root'):
        paths[key] = (audit_base if key in ('registry_root','approval_root') else base)/key
        paths[key].mkdir(mode=0o700)
    limits = resource()
    # Protocol-only budget includes the full pytest process, not a worker limit.
    limits.update(max_candidate_bytes=16777216, max_rss_bytes=4294967296, max_retained_candidates=100)
    config = dict(format_version=3,creator_uid=os.getuid(),approver_uid=os.getuid(),
                  resource_policy_sha=digest(canonical_bytes(limits)),expected_ddl_sha=PINNED_DDL_SHA)
    evidence = dict(format_version=1,evidence_id='e'*32,kernel='protocol',sqlite_version='protocol',
        compile_options=[],vfs='unix',filesystem='ext4',mount_id=1,mount_options=['rw'],
        namespace_id='protocol',approved_policy_sha='a'*64)
    def selected(c, r, role):
        entry = r['hard_limit_profiles'][role]
        return binding.RunnerBinding(role,entry['profile_id'],entry['profile_sha'],c['resource_policy_sha'],'a'*64)
    monkeypatch.setattr(binding,'resolve_runner_binding',selected)
    def prepare(pid, role, started):
        p = ownership._new_preparation(started+limits['max_elapsed_seconds'])
        p.started,p.role,p.config,p.resource = started,role,config,limits
        p.deployment_sha,p.roots = 'a'*64,{}
        for key,path in paths.items():
            p.roots[key] = os.open(path,os.O_RDONLY|os.O_DIRECTORY)
            p.owner.callback(os.close,p.roots[key])
        p.root_fd,p.lock,p.storage = None,None,None
        p.release = SimpleNamespace(commit='5'*40, revalidate=lambda:None)
        p.runner,p.platform_evidence,p.observation = selected(config,limits,role),evidence,{}
        return p
    monkeypatch.setattr(admission,'_prepare',prepare)
    monkeypatch.setattr(steps,'_candidate_quota_transport',lambda **kw: None)
    def install(p):
        p.boundary = {'candidate_id':p.candidate_id}
    monkeypatch.setattr(admission,'install',install)
    def verify(p, baseline=None):
        ownership._check_preparation(p)
        assert steps._scan_audit(p) == p.audit_snapshot
        return ()
    monkeypatch.setattr(admission,'verify_prepared',verify)
    monkeypatch.setattr(policy.BootstrapContext,'revalidate',lambda self:self.check_owner())
    monkeypatch.setattr(policy.BootstrapContext,'verify_candidate_quota',lambda *a:None)
    monkeypatch.setattr(policy.BootstrapContext,'verify_audit_boundary',lambda self,*a:self.check_owner())
    def approve(result, aid='4'*32):
        value = dict(format_version=2, approval_id=aid,candidate_id=result['candidate_id'],
            target_name=result['candidate_id'],manifest_sha=result['manifest_sha'],policy_sha='a'*64,
            scope='PUBLISH_EMPTY_UNACTIVATED',approver_reference='protocol-only',actor=selected(config,limits,'approver').actor())
        path=paths['approval_root']/(aid+'.json')
        path.write_bytes(canonical_bytes(value))
        path.chmod(0o600)
        return aid
    return SimpleNamespace(paths=paths, config=config, resource=limits, evidence=evidence, approve=approve,
        run=lambda operation,cid=None,aid=None:admission.execute_operation('a'*32,operation,cid,aid))


def test_v3_create_publish_recover(v3_protocol):
    h=v3_protocol
    created=h.run('create')
    assert created['status']=='VERIFIED',created
    cid=created['candidate_id']
    aid=h.approve(created)
    published=h.run('publish',cid,aid)
    assert published['status']=='PUBLISHED_UNACTIVATED',published
    assert not (h.paths['candidate_root']/cid).exists()
    assert (h.paths['publish_root']/cid/'display.sqlite').is_file()
    assert h.run('recover',cid)==published


@pytest.mark.parametrize('code,state', [('UNKNOWN_SCHEMA','FAILED'),('SIDECAR_REMAINS','FAILED'),
    ('IDENTITY_CHANGED','QUARANTINED'),('RESOURCE_LIMIT','FAILED')])
def test_v3_build_failure_retains_media(v3_protocol, monkeypatch, code, state):
    from api import display_bootstrap_artifact as artifact
    h=v3_protocol
    def reject(fd, **kwargs):
        raise ValueError(code)
    monkeypatch.setattr(artifact,'build_private_database',reject)
    result=h.run('create')
    assert result['code']==code,result
    cid=result['candidate_id']
    assert (h.paths['candidate_root']/cid).is_dir()
    assert not list(h.paths['publish_root'].iterdir())
    snapshot=inspect_log(h,cid)
    assert snapshot['registry_last']['state']==state
    assert snapshot['registry_last']['error_code']==code
    assert result['registry_seq']==snapshot['registry_last']['seq']


def inspect_log(h,cid):
    from api.display_bootstrap_prepared_audit import scan_prepared_audit
    import time
    fd=os.open(h.paths['registry_root']/cid,os.O_RDONLY|os.O_DIRECTORY)
    try:
        actor=dict(role='recover',**h.resource['hard_limit_profiles']['recover'])
        with scan_prepared_audit(directory_fd=fd,candidate_id=cid,deployment_sha='a'*64,
                resource=h.resource,actor=actor,deadline=time.monotonic()+30) as log:
            return log.inspect()
    finally:
        os.close(fd)


@pytest.mark.parametrize('stage',['quota','boundary'])
def test_v3_partial_preparation_recovers_without_candidate_requirement(v3_protocol,monkeypatch,stage):
    h=v3_protocol
    target,attr=(steps,'_candidate_quota_transport') if stage=='quota' else (admission,'install')
    original=getattr(target,attr)
    def reject(*args,**kwargs):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    monkeypatch.setattr(target,attr,reject)
    result=h.run('create')
    assert result['code']=='ACCESS_BOUNDARY_UNPROVEN',result
    cid=result['candidate_id']
    assert inspect_log(h,cid)['registry_last']['state']=='RESERVED'
    assert not (h.paths['candidate_root']/cid/'display.sqlite').exists()
    monkeypatch.setattr(target,attr,original)
    recovered=h.run('recover',cid)
    assert recovered['code']=='IO_FAILURE',recovered
    assert inspect_log(h,cid)['registry_last']['state']=='FAILED'
    assert recovered['registry_seq']==2


@pytest.mark.parametrize('scenario',['missing','wrong','rebind'])
def test_v3_approval_failure_preserves_media(v3_protocol,scenario):
    from api.display_bootstrap_manifest import parse_record
    h=v3_protocol
    created=h.run('create')
    assert created['status']=='VERIFIED',created
    cid=created['candidate_id']
    aid='4'*32
    if scenario!='missing':
        aid=h.approve(created)
    if scenario=='wrong':
        path=h.paths['approval_root']/(aid+'.json')
        value=parse_record(path.read_bytes())
        value['policy_sha']='9'*64
        path.write_bytes(canonical_bytes(value))
    if scenario=='rebind':
        assert h.run('publish',cid,aid)['status']=='PUBLISHED_UNACTIVATED'
        aid=h.approve(created,'8'*32)
    result=h.run('publish',cid,aid)
    assert result['status']=='BLOCKED',result
    assert result['code'] in ('APPROVAL_MISMATCH','POLICY_MISSING'),result
    root='publish_root' if scenario=='rebind' else 'candidate_root'
    assert (h.paths[root]/cid/'display.sqlite').is_file()


@pytest.mark.parametrize('failure',['quota','space','sqlite_full'])
def test_v3_resource_failure_preserves_partial_database(v3_protocol,monkeypatch,failure):
    import errno,sqlite3
    from api import display_bootstrap_artifact as artifact
    h=v3_protocol
    def exhaust(fd,**kwargs):
        f=os.open('partial.bin',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600,dir_fd=fd)
        try: os.write(f,b'retained')
        finally: os.close(f)
        if failure=='sqlite_full':
            exc=sqlite3.OperationalError('full')
            exc.sqlite_errorcode=sqlite3.SQLITE_FULL
            raise exc
        raise OSError(errno.EDQUOT if failure=='quota' else errno.ENOSPC,'full')
    monkeypatch.setattr(artifact,'build_private_database',exhaust)
    result=h.run('create')
    assert result['code']=='RESOURCE_LIMIT',result
    cid=result['candidate_id']
    assert (h.paths['candidate_root']/cid/'partial.bin').read_bytes()==b'retained'
    assert inspect_log(h,cid)['registry_last']['state']=='FAILED'


def test_v3_completion_failure_then_recovery(v3_protocol,monkeypatch):
    from api import display_bootstrap_fixed_registry as registry
    h=v3_protocol
    created=h.run('create')
    assert created['status']=='VERIFIED',created
    cid=created['candidate_id']
    aid=h.approve(created)
    original=registry.append_state
    def append(log,state,**kw):
        if state=='PUBLISHED_UNACTIVATED':
            raise OSError(5,'injected')
        return original(log,state,**kw)
    monkeypatch.setattr(registry,'append_state',append)
    result=h.run('publish',cid,aid)
    assert result['status']=='UNCERTAIN',result
    assert not (h.paths['candidate_root']/cid).exists()
    assert (h.paths['publish_root']/cid/'display.sqlite').exists()
    assert inspect_log(h,cid)['registry_last']['state']=='PUBLISH_INTENT'
    monkeypatch.setattr(registry,'append_state',original)
    recovered=h.run('recover',cid)
    assert recovered['status']=='PUBLISHED_UNACTIVATED',recovered


def test_v3_retention_limit_before_reservation(v3_protocol):
    h=v3_protocol
    h.resource['max_retained_candidates']=1
    (h.paths['publish_root']/('6'*32)).mkdir()
    result=h.run('create')
    assert result['code']=='RESOURCE_LIMIT',result
    assert result['candidate_id'] is None
    assert not list(h.paths['registry_root'].iterdir())


@pytest.mark.parametrize('expire_after',['audit_scan','approval','revalidation'])
def test_v3_expiry_before_next_write(v3_protocol,monkeypatch,expire_after):
    import time
    from api import display_bootstrap_fixed_registry as registry
    from api import display_bootstrap_fixed_lifecycle as lifecycle
    h=v3_protocol
    created=h.run('create'); cid=created['candidate_id']; aid=h.approve(created)
    original_time=time.monotonic
    clock=[original_time()]
    monkeypatch.setattr(time,'monotonic',lambda:clock[0])
    original_scan=steps._scan_audit
    original_append=registry.append_state
    original_check=policy.BootstrapContext.revalidate
    def scan(p):
        result=original_scan(p)
        if expire_after=='audit_scan': clock[0]+=61
        return result
    def append(log,state,**kw):
        result=original_append(log,state,**kw)
        if expire_after=='approval' and state=='APPROVED': clock[0]+=61
        return result
    def check(ctx):
        original_check(ctx)
        if expire_after=='revalidation': clock[0]+=61
    monkeypatch.setattr(steps,'_scan_audit',scan)
    monkeypatch.setattr(registry,'append_state',append)
    monkeypatch.setattr(policy.BootstrapContext,'revalidate',check)
    result=h.run('publish',cid,aid)
    assert result['status']=='BLOCKED',result
    assert result['code']=='RESOURCE_LIMIT',result
    assert (h.paths['candidate_root']/cid/'display.sqlite').exists()
    assert not (h.paths['publish_root']/cid).exists()
    monkeypatch.setattr(time,'monotonic',original_time)
    expected='APPROVED' if expire_after=='approval' else 'VERIFIED'
    assert inspect_log(h,cid)['registry_last']['state']==expected


@pytest.mark.parametrize('kind',['identity','exdev','enosys'])
def test_v3_publish_conflicts_preserve_source(v3_protocol,monkeypatch,kind):
    import errno
    from api import display_bootstrap_publish as storage
    h=v3_protocol
    created=h.run('create'); cid=created['candidate_id']; aid=h.approve(created)
    if kind=='identity':
        (h.paths['candidate_root']/cid).chmod(0o500)
    else:
        def reject(*a): raise OSError(errno.EXDEV if kind=='exdev' else errno.ENOSYS,'injected')
        monkeypatch.setattr(storage,'rename_no_replace',reject)
    result=h.run('publish',cid,aid)
    assert result['status']=='BLOCKED',result
    assert result['code']==('APPROVAL_MISMATCH' if kind=='identity' else 'UNSUPPORTED_PLATFORM'),result
    assert (h.paths['candidate_root']/cid).exists()
    assert not (h.paths['publish_root']/cid).exists()



def test_v3_durable_chain_and_evidence(v3_protocol):
    from api.display_bootstrap_manifest import parse_record
    from api.display_bootstrap_prepared_audit import scan_prepared_audit
    import time
    h=v3_protocol
    result=h.run('create'); assert result['status']=='VERIFIED',result
    cid=result['candidate_id']; path=h.paths['candidate_root']/cid
    assert {p.name for p in path.iterdir()}=={'display.sqlite','manifest.json'}
    raw=(path/'manifest.json').read_bytes(); manifest=parse_record(raw)
    assert digest(raw)==result['manifest_sha']
    fd=os.open(h.paths['registry_root']/cid,os.O_RDONLY|os.O_DIRECTORY)
    try:
        with scan_prepared_audit(directory_fd=fd,candidate_id=cid,deployment_sha='a'*64,
            resource=h.resource,actor=dict(role='creator',**h.resource['hard_limit_profiles']['creator']),
            deadline=time.monotonic()+30) as log:
            receipts=log.inspect()['receipts']
            states=[parse_record(log.get(1,r['logical_key']))['record']['state'] for r in receipts if r['kind']==1]
            assert states==['RESERVED','BUILDING','VERIFIED']
            payload=log.get(2,manifest['platform_evidence_sha'])
            assert digest(payload)==manifest['platform_evidence_sha']
            assert parse_record(payload)['record']==h.evidence
    finally: os.close(fd)


def test_v3_publication_guard_failure_preserves_verified(v3_protocol,monkeypatch):
    h=v3_protocol
    created=h.run('create'); cid=created['candidate_id']; aid=h.approve(created)
    def reject(*a): raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    monkeypatch.setattr(admission,'install',reject)
    result=h.run('publish',cid,aid)
    assert result['code']=='ACCESS_BOUNDARY_UNPROVEN',result
    assert inspect_log(h,cid)['registry_last']['state']=='VERIFIED'
    assert (h.paths['candidate_root']/cid/'display.sqlite').exists()


def test_v3_evidence_failure_never_verified(v3_protocol,monkeypatch):
    from api import display_bootstrap_fixed_registry as registry
    h=v3_protocol
    def reject(*a): raise ValueError('AUDIT_UNAVAILABLE')
    monkeypatch.setattr(registry,'persist_platform',reject)
    result=h.run('create'); cid=result['candidate_id']
    assert result['code']=='AUDIT_UNAVAILABLE',result
    assert (h.paths['candidate_root']/cid/'display.sqlite').exists()
    assert not (h.paths['candidate_root']/cid/'manifest.json').exists()
    assert inspect_log(h,cid)['registry_last']['state']=='FAILED'


def test_v3_failure_audit_omits_sensitive_exception_text(v3_protocol,monkeypatch):
    from api import display_bootstrap_artifact as artifact
    h=v3_protocol
    def reject(*a,**kw): raise OSError(5,'sensitive-private-path')
    monkeypatch.setattr(artifact,'build_private_database',reject)
    result=h.run('create'); cid=result['candidate_id']
    assert result['code']=='IO_FAILURE',result
    assert b'sensitive-private-path' not in (h.paths['registry_root']/cid/'audit.bin').read_bytes()
    assert inspect_log(h,cid)['registry_last']['state']=='FAILED'


def test_v3_creator_context_cannot_publish(v3_protocol,monkeypatch):
    from api import display_bootstrap_fixed_lifecycle as lifecycle
    original=lifecycle.create
    def create(ctx):
        result=original(ctx)
        assert result['status']=='VERIFIED',result
        blocked=lifecycle.publish(ctx,result['candidate_id'],'4'*32)
        assert blocked['code']=='ACCESS_BOUNDARY_UNPROVEN',blocked
        return result
    monkeypatch.setattr(lifecycle,'create',create)
    assert v3_protocol.run('create')['status']=='VERIFIED'


def test_v3_publisher_cannot_create(v3_protocol,monkeypatch):
    from api import display_bootstrap_fixed_lifecycle as lifecycle
    original=lifecycle.publish
    def publish(ctx,cid,aid):
        assert lifecycle.create(ctx)['code']=='ACCESS_BOUNDARY_UNPROVEN'
        return original(ctx,cid,aid)
    h=v3_protocol
    created=h.run('create'); aid=h.approve(created)
    monkeypatch.setattr(lifecycle,'publish',publish)
    assert h.run('publish',created['candidate_id'],aid)['status']=='PUBLISHED_UNACTIVATED'
