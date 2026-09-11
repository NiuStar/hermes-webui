"""Internal, non-serializable budget leases; evidence comes from the owner."""
import os
import secrets
import time
import weakref
from types import MappingProxyType
from api.display_bootstrap_v3 import hex_value
from api.display_bootstrap_audit_log import _deadline

_LIVE = weakref.WeakKeyDictionary()


def _starttime():
    with open('/proc/self/stat', encoding='ascii') as stream:
        return int(stream.read().rsplit(')', 1)[1].split()[19])


class GrowthLease:
    __slots__ = ('__weakref__',)

    def __new__(cls):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')

    def __reduce__(self):
        raise TypeError('growth lease cannot be serialized')


def _freeze(value):
    if type(value) is dict:
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if type(value) in (list, tuple):
        return tuple(_freeze(v) for v in value)
    if type(value) in (str, int, bool) or value is None:
        return value
    raise ValueError('INVALID_INPUT')


def issue(*, owner, candidate_id, operation, request_sha, bindings, observation,
          deadline, verify_owner, request_started_ns):
    """Internal factory. Caller must already hold and verify the actual locks.

    verify_owner is the trusted budget manager's bound method, not request data.
    This module cannot authenticate an authority or observe disk space itself.
    """
    hex_value(candidate_id, 32)
    hex_value(request_sha, 64)
    if operation not in ('create', 'publish', 'recover', 'approve') or not callable(verify_owner):
        raise ValueError('INVALID_INPUT')
    _deadline(deadline)
    verify_owner()
    now_ns = time.monotonic_ns()
    if (type(request_started_ns) is not int or not 0 <= request_started_ns <= now_ns
            or now_ns - request_started_ns >= 1_000_000_000):
        raise ValueError('RESOURCE_LIMIT')
    expires = min(deadline, (request_started_ns + 1_000_000_000) / 1e9)
    _deadline(expires)
    value = object.__new__(GrowthLease)
    _LIVE[value] = dict(owner=owner, pid=os.getpid(), starttime=_starttime(),
                       candidate_id=candidate_id, operation=operation, request_sha=request_sha,
                       bindings=_freeze(bindings), observation=_freeze(observation),
                       expires=expires, nonce=secrets.token_hex(16),
                       verify_owner=verify_owner)
    return value


def verify(value, *, owner, candidate_id, operation, request_sha, bindings):
    if type(value) is not GrowthLease:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    row = _LIVE.get(value)
    if (row is None or row['owner'] is not owner or row['pid'] != os.getpid()
            or row['starttime'] != _starttime()
            or (row['candidate_id'], row['operation'], row['request_sha']) !=
               (candidate_id, operation, request_sha) or row['bindings'] != _freeze(bindings)):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    _deadline(row['expires'])
    row['verify_owner']()
    _deadline(row['expires'])
    return row['observation']


def revoke(value):
    if type(value) is GrowthLease:
        _LIVE.pop(value, None)


def transfer(value, *, old_owner, new_owner, verify_owner):
    """Invalidate the old object immediately; only receipt consumption calls this."""
    if type(value) is not GrowthLease:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    row = _LIVE.pop(value, None)
    if (row is None or row['owner'] is not old_owner or row['pid'] != os.getpid()
            or row['starttime'] != _starttime() or not callable(verify_owner)):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    _deadline(row['expires'])
    row['verify_owner']()
    verify_owner()
    result = object.__new__(GrowthLease)
    _LIVE[result] = dict(row, owner=new_owner, verify_owner=verify_owner)
    return result
