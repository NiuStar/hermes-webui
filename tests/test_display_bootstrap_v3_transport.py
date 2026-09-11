"""Quota role boundary and mount-view contracts; queued for isolated testing."""
import copy
import stat
import pytest
from api.display_bootstrap_quota_protocol import validate_request
from api.display_bootstrap_storage_target import match_mount, compare_views


def request():
    return dict(version=2, operation='allocate', role='creator', deployment_sha='a'*64,
                resource_sha='b'*64, runner_profile_id='c'*32, runner_profile_sha='d'*64,
                candidate_id='e'*32, max_bytes=1024,
                directory_identity=dict(dev=1,ino=2,uid=999,gid=987,mode=stat.S_IFDIR|0o700))


@pytest.mark.parametrize('role', ['creator','publisher','recover'])
def test_verify_roles(role):
    value=request()
    value.update(role=role,operation='verify')
    assert validate_request(value)==value


@pytest.mark.parametrize('role', ['publisher','recover','approver'])
def test_allocate_only_creator(role):
    value=request()
    value['role']=role
    with pytest.raises(ValueError): validate_request(value)


@pytest.mark.parametrize('field,value', [('version',True),('version',1),('max_bytes',True),
                                        ('runner_profile_id','C'*32),('resource_sha','b'*63)])
def test_bad_wire_values(field,value):
    item=request()
    item[field]=value
    with pytest.raises(ValueError): validate_request(item)


def mount():
    return dict(mount_id=3,device=dict(major=7,minor=1),root='/',target='/audit',
                filesystem='ext4',options=['nodev','rw'])


def test_observer_readonly_does_not_relax_target(monkeypatch):
    target=mount()
    observer=copy.deepcopy(target)
    observer.update(mount_id=8,options=['ro'])
    monkeypatch.setattr('api.display_bootstrap_storage_target.mount_for_fd',
                        lambda fd,**kw: observer)
    result=compare_views({3:target},'/audit/registry',42,3,target['device'],['rw'])
    assert result['target']['mount_id']==3
    assert result['observer']['mount_id']==8
    target['options']=['ro']
    with pytest.raises(ValueError):
        compare_views({3:target},'/audit/registry',42,3,target['device'],['rw'])


def test_ambiguous_mount_rejected():
    first=mount()
    second=dict(first,mount_id=4)
    with pytest.raises(ValueError): match_mount({3:first,4:second},'/audit/registry')


def test_bind_root_rejected():
    item=mount()
    item['root']='/subdir'
    with pytest.raises(ValueError): match_mount({3:item},'/audit/registry')
