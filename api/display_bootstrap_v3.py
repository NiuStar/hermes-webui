"""Approved V3 metadata validation, without implicit legacy migration."""
import re

ROLES = ('creator', 'publisher', 'recover', 'approver')


def hex_value(value, size):
    if type(value) is not str or re.fullmatch('[0-9a-f]{%d}' % size, value) is None:
        raise ValueError('INVALID_INPUT')


def validate_actor(value, *, approval=False):
    if type(value) is not dict or set(value) != {'role', 'profile_id', 'profile_sha'}:
        raise ValueError('INVALID_INPUT')
    allowed = ('approver',) if approval else ROLES[:3]
    if value['role'] not in allowed:
        raise ValueError('INVALID_INPUT')
    hex_value(value['profile_id'], 32)
    hex_value(value['profile_sha'], 64)
    return value


def validate_mapping(value):
    if type(value) is not dict or set(value) != set(ROLES):
        raise ValueError('INVALID_INPUT')
    ids = set()
    for role in ROLES:
        entry = value[role]
        if type(entry) is not dict or set(entry) != {'profile_id', 'profile_sha'}:
            raise ValueError('INVALID_INPUT')
        hex_value(entry['profile_id'], 32)
        hex_value(entry['profile_sha'], 64)
        ids.add(entry['profile_id'])
    if len(ids) != len(ROLES):
        raise ValueError('INVALID_INPUT')
    return value


def validate(value, kind):
    from api.display_bootstrap_manifest import validate_record, canonical_bytes
    shared = dict(value)
    if kind == 'approval':
        validate_actor(shared.pop('actor', None), approval=True)
        if len(canonical_bytes(value)) > 60000:
            raise ValueError('INVALID_INPUT')
        shared['format_version'] = 1
        validate_record(shared, kind)
        return value
    if kind == 'resource':
        mapping = validate_mapping(shared.pop('hard_limit_profiles', None))
        if 'hard_limit_profile_id' in shared:
            raise ValueError('INVALID_INPUT')
        shared['hard_limit_profile_id'] = mapping['creator']['profile_id']
    elif kind == 'deployment':
        hex_value(shared.pop('observer_profile_id', None), 32)
        hex_value(shared.pop('observer_profile_sha', None), 64)
    elif kind != 'manifest':
        raise ValueError('INVALID_INPUT')
    if kind in ('resource', 'manifest'):
        if shared.get('audit_format') != 'FIXED_LOG_V2':
            raise ValueError('INVALID_INPUT')
        shared['audit_format'] = 'FIXED_LOG_V1'
    # Validate shared fields using a private copy; never rewrite supplied bytes.
    shared['format_version'] = 2
    validate_record(shared, kind)
    return value
