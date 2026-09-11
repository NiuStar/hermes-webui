"""Observer-owned measurements, never client-independent process evidence."""
import os
import re
from api.display_bootstrap_audit_log import _deadline
from api.display_bootstrap_observer_release import verify_file


def validate_observation(value, profile):
    if type(value) is not dict or set(value) != {
            'user_namespace', 'cgroup_namespace', 'mount_namespace', 'executable_identity'}:
        raise ValueError('INVALID_INPUT')
    constraints = profile['namespace_constraints']
    for key, prefix in (('user_namespace', 'user'), ('cgroup_namespace', 'cgroup'),
                        ('mount_namespace', 'mnt')):
        if type(value[key]) is not str or re.fullmatch(prefix + r':\[[0-9]+\]', value[key]) is None:
            raise ValueError('INVALID_INPUT')
    if (value['user_namespace'] != constraints['user_namespace']
            or value['cgroup_namespace'] != constraints['cgroup_namespace']
            or value['executable_identity'] != profile['python_executable_identity']):
        raise ValueError('APPROVAL_MISMATCH')
    return value


def observe_self(profile, deadline):
    _deadline(deadline)
    if os.geteuid() != 0:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    executable = profile['python_executable_identity']
    before = os.stat('/proc/self/exe')
    if (os.readlink('/proc/self/exe') != executable['path']
            or {key: getattr(before, 'st_' + key) for key in executable['identity']}
            != executable['identity']):
        raise ValueError('IDENTITY_CHANGED')
    verify_file(executable['path'], executable['identity'], executable['sha'], deadline)
    value = dict(user_namespace=os.readlink('/proc/self/ns/user'),
                 cgroup_namespace=os.readlink('/proc/self/ns/cgroup'),
                 mount_namespace=os.readlink('/proc/self/ns/mnt'),
                 executable_identity=executable)
    after = os.stat('/proc/self/exe')
    if before != after:
        raise ValueError('IDENTITY_CHANGED')
    _deadline(deadline)
    return validate_observation(value, profile)
