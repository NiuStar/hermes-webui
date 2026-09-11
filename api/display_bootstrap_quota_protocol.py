"""Strict quota V2 binding shared by both endpoints; no legacy write fallback."""
from api.display_bootstrap_v3 import hex_value

BINDING = {'deployment_sha', 'resource_sha', 'role', 'runner_profile_id', 'runner_profile_sha'}
REQUEST = BINDING | {'version', 'operation', 'candidate_id', 'directory_identity', 'max_bytes'}


def validate_request(value):
    from api.display_bootstrap_manifest import validate_record
    if type(value) is not dict or set(value) != REQUEST:
        raise ValueError('INVALID_INPUT')
    if (type(value['version']) is not int or value['version'] != 2
            or value['role'] not in ('creator', 'publisher', 'recover')
            or value['operation'] not in ('allocate', 'verify')):
        raise ValueError('INVALID_INPUT')
    if value['operation'] == 'allocate' and value['role'] != 'creator':
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    for key in BINDING - {'role'}:
        hex_value(value[key], 32 if key.endswith('_id') else 64)
    hex_value(value['candidate_id'], 32)
    validate_record(value['directory_identity'], 'directory_identity')
    if type(value['max_bytes']) is not int or not 0 < value['max_bytes'] <= 9007199254740991:
        raise ValueError('INVALID_INPUT')
    return value
