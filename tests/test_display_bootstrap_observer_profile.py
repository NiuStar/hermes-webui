"""Observer profile rejection matrix, not live service qualification."""
import copy
import stat
import pytest
from api.display_bootstrap_observer_profile import validate_profile, PROPERTIES, LIMITS


def profile():
    return dict(format_version=1,observer_profile_id='a'*32,
        unit_template='hermes-bootstrap-storage@.service',socket_unit='hermes-bootstrap-storage.socket',
        release_manifest_sha='b'*64,python_executable_identity=dict(path='/usr/bin/python3.11',sha='c'*64,
            identity=dict(dev=1,ino=1,uid=0,gid=0,mode=stat.S_IFREG|0o755,nlink=1)),
        service_properties=copy.deepcopy(PROPERTIES),resource_limits=dict(LIMITS),
        namespace_constraints=dict(user_namespace='user:[1]',cgroup_namespace='cgroup:[2]',
            target_mount_namespace='mnt:[3]',observer_mount_mode='private-stable'))


def test_valid_profile_preserved():
    value=profile()
    before=copy.deepcopy(value)
    assert validate_profile(value)==before
    assert value==before


@pytest.mark.parametrize('key,value',[('User',False),('NoNewPrivileges',1),('PrivateTmp',False),('Delegate',True)])
def test_property_type_or_permission_rejected(key,value):
    item=profile()
    item['service_properties'][key]=value
    with pytest.raises(ValueError): validate_profile(item)


@pytest.mark.parametrize('path',['relative','/usr/../bin/python','/usr//bin/python','/usr/bin/python/'])
def test_executable_path_rejected(path):
    item=profile()
    item['python_executable_identity']['path']=path
    with pytest.raises(ValueError): validate_profile(item)


def test_unknown_field_rejected():
    item=profile()
    item['trusted']=True
    with pytest.raises(ValueError): validate_profile(item)
