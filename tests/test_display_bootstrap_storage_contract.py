import copy
import stat
import pytest
from api.display_bootstrap_storage_contract import validate_contract, validate_acceptance


def contract():
    return dict(format_version=1, storage_contract_id='a'*32, volume_profile_sha='b'*64,
                namespace_id='mnt:[123]', audit_topology=dict(
                    loop_device={'major':7,'minor':1}, loop_mount_id=40,
                    loop_fs_uuid='11111111-1111-1111-1111-111111111111',
                    backing_identity=dict(dev=1,ino=2,uid=0,gid=0,mode=stat.S_IFREG|0o600,nlink=1),
                    backing_bytes=4096, host_mount_id=30, host_device={'major':254,'minor':1},
                    host_fs_uuid='22222222-2222-2222-2222-222222222222',
                    host_disk_device={'major':254,'minor':0},host_partition_start_sectors=2048,
                    block_bytes=4096,required_mount_options=['rw']),
                administrator_assurance=dict(deployment_reference='qualification',responsible_party='admin',
                    no_overcommit=True,no_snapshot_rollback=True,flush_honored=True,
                    unobservable_layers=['hypervisor']),
                acceptance=dict(code_commit='c'*40,kernel_release='test',volume_profile_sha='b'*64,
                                report_sha='d'*64,report_id='e'*32))


def test_contract_validates_without_claiming_authority():
    value = contract()
    assert validate_contract(value) == value


@pytest.mark.parametrize('key', ['no_overcommit','no_snapshot_rollback','flush_honored'])
def test_missing_assurance_rejected(key):
    value = contract()
    value['administrator_assurance'][key] = False
    with pytest.raises(ValueError):
        validate_contract(value)


def test_unknown_fields_and_boolean_integer():
    value = contract()
    with pytest.raises(ValueError):
        validate_contract(dict(value, skipped=True))
    value['audit_topology']['backing_bytes'] = True
    with pytest.raises(ValueError):
        validate_contract(value)


def test_incomplete_acceptance_rejected():
    value = dict(format_version=1,report_id='e'*32,code_commit='c'*40,kernel_release='test',
                 volume_profile_sha='b'*64,topology_sha='f'*64,checks={})
    with pytest.raises(ValueError):
        validate_acceptance(value)
    value['checks'] = {k:dict(status='PASS',evidence_sha='a'*64) for k in
        ('audit_blocks_full','audit_inodes_full','host_blocks_full','lifecycle_failure_close',
         'crash_recovery','sync_error','real_role_channels')}
    assert validate_acceptance(value) == value
    other = copy.deepcopy(value)
    other['checks']['real_role_channels']['status'] = 'SKIP'
    with pytest.raises(ValueError):
        validate_acceptance(other)
