"""Strict RESULT schema and comparison against local, held storage evidence."""
import re
from api.display_bootstrap_audit_log import _deadline
from api.display_bootstrap_observer_self import validate_observation


def fields(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        raise ValueError('INVALID_INPUT')


def integer(value, minimum=0, maximum=2**53-1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('INVALID_INPUT')


def device(value):
    fields(value, 'major minor')
    for number in value.values():
        integer(number, maximum=2**32-1)


def journal(value):
    fields(value, 'device fs_uuid block_bytes blocks internal_journal journal_inode errors state '
                 'needs_recovery features inode_bytes blocks_per_group inodes_per_group journal_uuid '
                 'journal_block_bytes journal_max_blocks journal_features')
    device(value['device'])
    for key in ('fs_uuid', 'journal_uuid'):
        item = value[key]
        if (type(item) is not str or re.fullmatch(
                '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', item) is None
                or item == '00000000-0000-0000-0000-000000000000'):
            raise ValueError('INVALID_INPUT')
    for key in ('block_bytes', 'journal_block_bytes'):
        integer(value[key], 4096, 4096)
    integer(value['blocks'], 1)
    integer(value['errors'], 0, 0)
    integer(value['state'], 0, 1)
    for key in ('journal_inode', 'blocks_per_group', 'inodes_per_group', 'journal_max_blocks'):
        integer(value[key], 1, 2**32-1)
    integer(value['inode_bytes'], 128, 4096)
    if value['inode_bytes'] & (value['inode_bytes'] - 1):
        raise ValueError('INVALID_INPUT')
    for key in ('features', 'journal_features'):
        fields(value[key], 'compat incompat readonly')
        for number in value[key].values():
            integer(number, maximum=2**32-1)
    incompat = value['features']['incompat']
    if (value['internal_journal'] is not True or type(value['needs_recovery']) is not bool
            or value['needs_recovery'] != bool(incompat & 4)
            or not value['features']['compat'] & 4
            or incompat & (0x10 | 1 | 8 | 0x8000 | 0x10000)
            or value['journal_uuid'] != value['fs_uuid']):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')


def mount(value):
    fields(value, 'mount_id device root target filesystem options')
    integer(value['mount_id'], 1)
    device(value['device'])
    for key in ('root', 'target'):
        item = value[key]
        if (type(item) is not str or not item.startswith('/') or '\x00' in item
                or len(item.encode()) > 4096):
            raise ValueError('INVALID_INPUT')
    options = value['options']
    if (type(options) is not list or not 1 <= len(options) <= 256
            or any(type(v) is not str or not v or '\x00' in v or len(v.encode()) > 256 for v in options)
            or options != sorted(set(options))):
        raise ValueError('INVALID_INPUT')
    if value['root'] != '/' or value['filesystem'] != 'ext4':
        raise ValueError('UNSUPPORTED_PLATFORM')


def validate_storage_observation(observation, *, storage_handle, observer_evidence,
                                 local_evidence, deadline):
    _deadline(deadline)
    storage_handle.revalidate()
    fields(observation, 'loop host views observer_self')
    contract = storage_handle.snapshot('contract')
    volume = storage_handle.snapshot('volume')
    topology = contract['audit_topology']
    fields(observation['views'], 'loop host')
    for name in ('loop', 'host'):
        item = observation[name]
        journal(item)
        if item['device'] != topology[name + '_device'] or item['fs_uuid'] != topology[name + '_fs_uuid']:
            raise ValueError('APPROVAL_MISMATCH')
        view = observation['views'][name]
        fields(view, 'target observer')
        for side in view.values():
            mount(side)
        target, other = view['target'], view['observer']
        if (target != local_evidence[name] or target['mount_id'] != topology[name + '_mount_id']
                or target['device'] != topology[name + '_device']
                or 'rw' not in target['options'] or 'ro' in target['options']
                or any(other[k] != target[k] for k in ('device', 'root', 'target', 'filesystem'))):
            raise ValueError('IDENTITY_CHANGED')
    if (not set(topology['required_mount_options']) <= set(observation['views']['loop']['target']['options'])
            or observation['loop']['blocks'] * 4096 > volume['audit']['image_bytes']):
        raise ValueError('APPROVAL_MISMATCH')
    validate_observation(observation['observer_self'], observer_evidence['profile'])
    for key in ('user_namespace', 'cgroup_namespace', 'mount_namespace'):
        if len(observation['observer_self'][key].encode()) > 128:
            raise ValueError('INVALID_INPUT')
    storage_handle.revalidate()
    _deadline(deadline)
    return observation
