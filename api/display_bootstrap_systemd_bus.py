"""Typed system-manager property reads with a shared absolute deadline."""
import json
import subprocess
import time
from api.display_bootstrap_audit_log import _deadline

DESTINATION = 'org.freedesktop.systemd1'
MANAGER = '/org/freedesktop/systemd1'


def call(arguments, deadline):
    _deadline(deadline)
    result = subprocess.run(['/usr/bin/busctl', '--system', '--json=short', *arguments],
        capture_output=True, check=True, timeout=max(0.001, deadline-time.monotonic()),
        env={'LC_ALL':'C','PATH':'/usr/bin:/bin'})
    _deadline(deadline)
    if len(result.stdout) > 262144:
        raise ValueError('RESOURCE_LIMIT')
    value = json.loads(result.stdout)
    if type(value) is not dict or set(value) != {'type', 'data'}:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    return value


def unit_for_pid(pid, deadline):
    if type(pid) is not int or not 0 < pid <= 4294967295:
        raise ValueError('INVALID_INPUT')
    result = call(['call', DESTINATION, MANAGER, DESTINATION+'.Manager',
                   'GetUnitByPID', 'u', str(pid)], deadline)
    if (result['type'] != 'o' or type(result['data']) is not list
            or len(result['data']) != 1 or type(result['data'][0]) is not str
            or not result['data'][0].startswith(MANAGER+'/unit/')):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    return result['data'][0]


def property_value(path, interface, name, signature, deadline):
    result = call(['get-property', DESTINATION, path, DESTINATION+'.'+interface, name], deadline)
    if result['type'] != signature:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    data = result['data']
    if signature == 's' and type(data) is not str:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    if signature == 'b' and type(data) is not bool:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    ranges = {'u': (0, 2**32-1), 't': (0, 2**64-1), 'i': (-2**31, 2**31-1)}
    if signature in ranges:
        low, high = ranges[signature]
        if type(data) is not int or not low <= data <= high:
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    if signature == '(bas)':
        if (type(data) is not list or len(data) != 2 or type(data[0]) is not bool
                or type(data[1]) is not list or any(type(v) is not str for v in data[1])):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    if signature in ('as','ay'):
        if type(data) is not list:
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        if signature == 'as' and any(type(v) is not str for v in data):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        if signature == 'ay' and any(type(v) is not int or not 0 <= v <= 255 for v in data):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    return data
