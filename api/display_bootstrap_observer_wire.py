"""Strict V3 observer frame schemas. Validation never grants a growth lease."""
from api.display_bootstrap_manifest import canonical_bytes, digest, validate_record
from api.display_bootstrap_storage_handshake import BINDING_FIELDS
from api.display_bootstrap_storage_observation import fields, integer, device
from api.display_bootstrap_v3 import hex_value

OPERATIONS = dict(creator='create', publisher='publish', recover='recover', approver='approve')
COMMON = BINDING_FIELDS | {'version', 'type', 'batch_id', 'connection_nonce', 'nonce'}
OPERATION = {'candidate_id', 'operation', 'request_sha'}
EXTRAS = {
    'HELLO': set(), 'READY': set(), 'BEGIN': set(), 'BOUND': set(),
    'OBSERVE': {'seq', 'previous_result_sha'},
    'RESULT': {'seq', 'previous_result_sha', 'sample', 'sample_sha'},
    'ACK': {'seq', 'result_sha'}, 'COMMITTED': {'seq', 'result_sha'},
    'CLOSE': {'last_seq', 'last_result_sha'}, 'CLOSED': {'last_seq', 'last_result_sha'},
}


def validate_sample(value, validate_storage):
    fields(value, 'format_version started_ns finished_ns storage capacity')
    integer(value['format_version'], 1, 1)
    integer(value['started_ns'])
    integer(value['finished_ns'], value['started_ns'])
    fields(value['storage'], 'loop host views observer_self')
    validate_storage(value['storage'])
    fields(value['capacity'], 'host audit')
    import re
    for item in value['capacity'].values():
        fields(item, 'fs_uuid device root_identity free_bytes free_inodes')
        uuid = item['fs_uuid']
        if (type(uuid) is not str or re.fullmatch(
                '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', uuid) is None
                or uuid == '00000000-0000-0000-0000-000000000000'):
            raise ValueError('INVALID_INPUT')
        device(item['device'])
        validate_record(item['root_identity'], 'directory_identity')
        integer(item['free_bytes'])
        integer(item['free_inodes'])
    if len(canonical_bytes(value)) > 60000:
        raise ValueError('RESOURCE_LIMIT')
    return value


def validate(value):
    if type(value) is not dict or type(value.get('type')) is not str:
        raise ValueError('INVALID_INPUT')
    kind = value['type']
    if kind not in EXTRAS:
        raise ValueError('INVALID_INPUT')
    expected = COMMON | EXTRAS[kind]
    if kind != 'HELLO':
        expected |= {'server_nonce'}
    if kind not in ('HELLO', 'READY'):
        expected |= OPERATION
    if set(value) != expected:
        raise ValueError('INVALID_INPUT')
    integer(value['version'], 3, 3)
    if type(value['role']) is not str or value['role'] not in OPERATIONS:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    for key in BINDING_FIELDS - {'role'}:
        hex_value(value[key], 32 if key.endswith('_id') else 64)
    for key in ('batch_id', 'connection_nonce', 'nonce', 'server_nonce', 'candidate_id'):
        if key in value:
            hex_value(value[key], 32)
    if 'operation' in value:
        if value['operation'] != OPERATIONS[value['role']]:
            raise ValueError('APPROVAL_MISMATCH')
        hex_value(value['request_sha'], 64)
    if 'seq' in value:
        integer(value['seq'], 1, 1024)
    if 'previous_result_sha' in value:
        if value['seq'] == 1:
            if value['previous_result_sha'] is not None:
                raise ValueError('APPROVAL_MISMATCH')
        else:
            hex_value(value['previous_result_sha'], 64)
    if 'result_sha' in value:
        hex_value(value['result_sha'], 64)
    if 'last_seq' in value:
        integer(value['last_seq'], 0, 1024)
        if value['last_seq'] == 0:
            if value['last_result_sha'] is not None:
                raise ValueError('APPROVAL_MISMATCH')
        else:
            hex_value(value['last_result_sha'], 64)
    if kind == 'RESULT':
        hex_value(value['sample_sha'], 64)
        if digest(canonical_bytes(value['sample'])) != value['sample_sha']:
            raise ValueError('APPROVAL_MISMATCH')
    canonical_bytes(value)
    return value


def frame(kind, common, **extra):
    return validate(dict(common, type=kind, **extra))


def expect(value, kind, common, **expected):
    validate(value)
    if value['type'] != kind or any(value.get(k) != v for k, v in dict(common, **expected).items()):
        raise ValueError('APPROVAL_MISMATCH')
    return value
