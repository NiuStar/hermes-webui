"""Strict observer configuration; records alone grant no runtime authority."""
import re
import stat
from api.display_bootstrap_manifest import canonical_bytes, validate_record
from api.display_bootstrap_v3 import hex_value

PROPERTIES = dict(Type='exec', User=0, Group=0, Restart='no', Delegate=False,
    NoNewPrivileges=True, KillMode='control-group', KillSignal=9, FinalKillSignal=9,
    SendSIGKILL=True, ProtectSystem='strict', ProtectHome='read-only', PrivateTmp=True,
    PrivateDevices=False, RestrictAddressFamilies=['AF_UNIX'], UMask='0077')
LIMITS = dict(memory_max_bytes=134217728, memory_swap_max_bytes=0,
              pids_max=8, runtime_max_usec=15000000)


def object_fields(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError('INVALID_INPUT')


def path_value(value, *, relative=False):
    if (type(value) is not str or not value or len(value.encode()) > 4096
            or '\x00' in value or value.startswith('/') == relative
            or any(p in ('', '.', '..') for p in value.split('/')[0 if relative else 1:])):
        raise ValueError('INVALID_INPUT')
    return value


def identity(value):
    validate_record(value, 'file_identity')
    if (any(type(n) is not int or not 0 <= n <= 9007199254740991 for n in value.values())
            or value['uid'] != 0 or value['mode'] & 0o022
            or not stat.S_ISREG(value['mode']) or value['nlink'] != 1):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')


def validate_profile(value):
    canonical_bytes(value)
    object_fields(value, ('format_version','observer_profile_id','unit_template','socket_unit',
        'release_manifest_sha','python_executable_identity','service_properties','resource_limits',
        'namespace_constraints'))
    if type(value['format_version']) is not int or value['format_version'] != 1:
        raise ValueError('INVALID_INPUT')
    hex_value(value['observer_profile_id'],32)
    hex_value(value['release_manifest_sha'],64)
    if (value['unit_template'] != 'hermes-bootstrap-storage@.service'
            or value['socket_unit'] != 'hermes-bootstrap-storage.socket'):
        raise ValueError('INVALID_INPUT')
    executable=value['python_executable_identity']
    object_fields(executable, ('path','sha','identity'))
    path_value(executable['path'])
    hex_value(executable['sha'],64)
    identity(executable['identity'])
    for key, expected in (('service_properties', PROPERTIES), ('resource_limits', LIMITS)):
        if canonical_bytes(value[key]) != canonical_bytes(expected):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    ns=value['namespace_constraints']
    object_fields(ns, ('user_namespace','cgroup_namespace','target_mount_namespace','observer_mount_mode'))
    for key,prefix in (('user_namespace','user'),('cgroup_namespace','cgroup'),('target_mount_namespace','mnt')):
        if type(ns[key]) is not str or re.fullmatch(prefix+r':\[[0-9]+\]',ns[key]) is None:
            raise ValueError('INVALID_INPUT')
    if ns['observer_mount_mode'] != 'private-stable':
        raise ValueError('INVALID_INPUT')
    return value


def validate_release(value):
    canonical_bytes(value)
    object_fields(value, ('format_version','release_root','files'))
    if type(value['format_version']) is not int or value['format_version'] != 1:
        raise ValueError('INVALID_INPUT')
    path_value(value['release_root'])
    files=value['files']
    if type(files) is not list or not 1 <= len(files) <= 256:
        raise ValueError('INVALID_INPUT')
    paths=[]
    for item in files:
        object_fields(item, ('path','sha','identity'))
        path_value(item['path'],relative=True)
        hex_value(item['sha'],64)
        identity(item['identity'])
        paths.append(item['path'])
    if paths != sorted(set(paths)) or 'scripts/bootstrap_storage_entry.py' not in paths:
        raise ValueError('INVALID_INPUT')
    return value
