"""Strict offline operation result boundary; no state or authority is inferred."""
import re
from api.display_bootstrap_manifest import canonical_bytes


ERROR_CODES = frozenset({
    'INVALID_INPUT', 'NONCANONICAL_RECORD', 'POLICY_MISSING',
    'ACCESS_BOUNDARY_UNPROVEN', 'IDENTITY_CHANGED', 'LOCK_BUSY',
    'UNSUPPORTED_PLATFORM', 'RESOURCE_LIMIT', 'APPROVAL_MISMATCH',
    'UNKNOWN_SCHEMA', 'SIDECAR_REMAINS', 'TARGET_CONFLICT',
    'STATE_CONFLICT', 'IO_FAILURE', 'AUDIT_UNAVAILABLE',
})


def validate_result(value, *, operation, candidate_id=None):
    canonical_bytes(value)
    if operation not in ('create', 'publish', 'recover') or type(value) is not dict:
        raise ValueError('INVALID_INPUT')
    common = {'format_version', 'status', 'code', 'candidate_id', 'target_name', 'registry_seq'}
    status = value.get('status')
    extras = {'BLOCKED': set(), 'UNCERTAIN': set(),
              'QUARANTINED': {'quarantine_persisted'},
              'VERIFIED': {'manifest_sha'},
              'PUBLISHED_UNACTIVATED': {'manifest_sha', 'completion_record_sha'}}
    if type(status) is not str or status not in extras or set(value) != common | extras[status]:
        raise ValueError('INVALID_INPUT')
    if type(value['format_version']) is not int or value['format_version'] != 1:
        raise ValueError('INVALID_INPUT')
    cid = value['candidate_id']
    if cid is not None and (type(cid) is not str or re.fullmatch('[0-9a-f]{32}', cid) is None):
        raise ValueError('INVALID_INPUT')
    if value['target_name'] != cid or (candidate_id is not None and cid != candidate_id):
        raise ValueError('INVALID_INPUT')
    seq = value['registry_seq']
    if seq is not None and (type(seq) is not int or seq < 1 or cid is None):
        raise ValueError('INVALID_INPUT')
    success = status in ('VERIFIED', 'PUBLISHED_UNACTIVATED')
    if type(value['code']) is not str:
        raise ValueError('INVALID_INPUT')
    if success:
        if value['code'] != 'OK' or cid is None or seq is None:
            raise ValueError('INVALID_INPUT')
        if (status == 'VERIFIED') != (operation == 'create'):
            raise ValueError('INVALID_INPUT')
        for key in extras[status]:
            if type(value[key]) is not str or re.fullmatch('[0-9a-f]{64}', value[key]) is None:
                raise ValueError('INVALID_INPUT')
    elif value['code'] not in ERROR_CODES:
        raise ValueError('INVALID_INPUT')
    if status == 'UNCERTAIN' and (operation == 'create' or cid is None or value['code'] != 'IO_FAILURE'):
        raise ValueError('INVALID_INPUT')
    if status == 'QUARANTINED':
        if (cid is None or type(value['quarantine_persisted']) is not bool
                or value['code'] not in ('IDENTITY_CHANGED', 'STATE_CONFLICT')
                or (value['quarantine_persisted'] and seq is None)):
            raise ValueError('INVALID_INPUT')
    return value
