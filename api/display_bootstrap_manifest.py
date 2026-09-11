"""Canonical metadata bytes for the unwired offline bootstrap lifecycle."""
import hashlib
import json
import re


def validate_record(value, kind):
    canonical_bytes(value)
    if type(value) is dict and type(value.get('format_version')) is int:
        if kind == 'resource' and value['format_version'] == 4:
            from api.display_bootstrap_v4 import validate_resource
            return validate_resource(value)
        if kind == 'registry' and value['format_version'] == 2:
            from api.display_bootstrap_v4 import validate_registry
            return validate_registry(value)
    if (type(value) is dict and type(value.get('format_version')) is int
            and ((value['format_version'] == 3 and kind in ('resource', 'manifest', 'deployment'))
                 or (value['format_version'] == 2 and kind == 'approval'))):
        from api.display_bootstrap_v3 import validate
        return validate(value, kind)
    if (type(value) is dict and type(value.get('format_version')) is int
            and value['format_version'] == 2 and kind in ('resource', 'manifest', 'deployment')):
        return _validate_v2(value, kind)
    if kind in ('directory_identity', 'file_identity'):
        import stat
        expected = {'dev', 'ino', 'uid', 'gid', 'mode'}
        if kind == 'file_identity':
            expected.add('nlink')
        if (type(value) is not dict or set(value) != expected
                or any(type(v) is not int for v in value.values()) or value['ino'] < 1):
            raise ValueError('INVALID_INPUT')
        if kind == 'file_identity':
            valid = stat.S_ISREG(value['mode']) and value['nlink'] == 1
        else:
            valid = stat.S_ISDIR(value['mode'])
        if not valid:
            raise ValueError('INVALID_INPUT')
        return value
    if kind == 'resource':
        expected = {'format_version', 'max_candidate_bytes', 'min_free_bytes',
                    'max_retained_candidates', 'max_rss_bytes', 'max_elapsed_seconds',
                    'check_interval_ms', 'audit_reserve_bytes', 'hard_limit_profile_id'}
        if type(value) is not dict or set(value) != expected:
            raise ValueError('INVALID_INPUT')
        if any(type(value[k]) is not int or value[k] < 1
               for k in expected - {'hard_limit_profile_id'}):
            raise ValueError('INVALID_INPUT')
        if (value['format_version'] != 1
                or type(value['hard_limit_profile_id']) is not str
                or re.fullmatch('[0-9a-f]{32}', value['hard_limit_profile_id']) is None
                or value['check_interval_ms'] > value['max_elapsed_seconds'] * 1000):
            raise ValueError('INVALID_INPUT')
        return value
    if kind == 'registry':
        expected = {'format_version', 'candidate_id', 'seq', 'previous_sha', 'state',
                    'target_name', 'manifest_sha', 'approval_id', 'error_code'}
        if (type(value) is not dict or set(value) != expected
                or type(value['format_version']) is not int or value['format_version'] != 1
                or type(value['seq']) is not int or value['seq'] < 1):
            raise ValueError('INVALID_INPUT')
        for key, length in [('candidate_id', 32), ('target_name', 32),
                            ('previous_sha', 64), ('manifest_sha', 64), ('approval_id', 32)]:
            if value[key] is None and key in ('previous_sha', 'manifest_sha', 'approval_id'):
                continue
            if type(value[key]) is not str or re.fullmatch('[0-9a-f]{%d}' % length, value[key]) is None:
                raise ValueError('INVALID_INPUT')
        if value['state'] not in ('RESERVED', 'BUILDING', 'VERIFIED', 'APPROVED',
                                  'PUBLISH_INTENT', 'PUBLISHED_UNACTIVATED', 'FAILED', 'QUARANTINED'):
            raise ValueError('INVALID_INPUT')
        if value['target_name'] != value['candidate_id']:
            raise ValueError('INVALID_INPUT')
        if (value['seq'] == 1) != (value['previous_sha'] is None):
            raise ValueError('INVALID_INPUT')
        state = value['state']
        if state in ('RESERVED', 'BUILDING') and (value['manifest_sha'] is not None or value['approval_id'] is not None):
            raise ValueError('INVALID_INPUT')
        if state == 'VERIFIED' and (value['manifest_sha'] is None or value['approval_id'] is not None):
            raise ValueError('INVALID_INPUT')
        if state in ('APPROVED', 'PUBLISH_INTENT', 'PUBLISHED_UNACTIVATED') and (value['manifest_sha'] is None or value['approval_id'] is None):
            raise ValueError('INVALID_INPUT')
        errors = ('INVALID_INPUT', 'NONCANONICAL_RECORD', 'UNSUPPORTED_PLATFORM',
                  'ACCESS_BOUNDARY_UNPROVEN', 'LOCK_BUSY', 'POLICY_MISSING',
                  'RESOURCE_LIMIT', 'AUDIT_UNAVAILABLE', 'UNKNOWN_SCHEMA',
                  'SIDECAR_REMAINS', 'IDENTITY_CHANGED', 'APPROVAL_MISMATCH',
                  'TARGET_CONFLICT', 'STATE_CONFLICT', 'IO_FAILURE', 'LEGACY_INITIALIZER_DISABLED')
        if state in ('FAILED', 'QUARANTINED'):
            if value['error_code'] not in errors:
                raise ValueError('INVALID_INPUT')
        elif value['error_code'] is not None:
            raise ValueError('INVALID_INPUT')
        return value
    if kind == 'manifest':
        expected = {'format_version', 'candidate_id', 'ddl_sha', 'sqlite_version',
                    'platform_evidence_sha', 'resource_policy_sha', 'db_bytes', 'db_sha',
                    'db_identity', 'candidate_directory_identity', 'connection_policy',
                    'verification', 'creator_commit'}
        if (type(value) is not dict or set(value) != expected
                or type(value['format_version']) is not int or value['format_version'] != 1
                or type(value['db_bytes']) is not int or value['db_bytes'] < 1
                or type(value['sqlite_version']) is not str):
            raise ValueError('INVALID_INPUT')
        for key in ('candidate_id', 'ddl_sha', 'platform_evidence_sha', 'resource_policy_sha', 'db_sha'):
            length = 32 if key == 'candidate_id' else 64
            if type(value[key]) is not str or re.fullmatch('[0-9a-f]{%d}' % length, value[key]) is None:
                raise ValueError('INVALID_INPUT')
        if type(value['creator_commit']) is not str or re.fullmatch('([0-9a-f]{40}|[0-9a-f]{64})', value['creator_commit']) is None:
            raise ValueError('INVALID_INPUT')
        if value['ddl_sha'] != '578f80789de98456324d430142631c7b7f984f80f3ffe30fc7c31d0634bdcd9d':
            raise ValueError('INVALID_INPUT')
        validate_record(value['db_identity'], 'file_identity')
        validate_record(value['candidate_directory_identity'], 'directory_identity')
        if (canonical_bytes(value['connection_policy']) != b'{"foreign_keys":1,"synchronous":2}'
                or canonical_bytes(value['verification']) != b'{"empty_state":true,"foreign_key_violations":0,"integrity_check":"ok"}'):
            raise ValueError('INVALID_INPUT')
        return value
    if kind == 'platform_evidence':
        expected = {'format_version', 'evidence_id', 'kernel', 'sqlite_version',
                    'compile_options', 'vfs', 'filesystem', 'mount_id', 'mount_options',
                    'namespace_id', 'approved_policy_sha'}
        if (type(value) is not dict or set(value) != expected
                or type(value['format_version']) is not int or value['format_version'] != 1
                or type(value['mount_id']) is not int):
            raise ValueError('INVALID_INPUT')
        for key in ('kernel', 'sqlite_version', 'vfs', 'filesystem', 'namespace_id'):
            if type(value[key]) is not str:
                raise ValueError('INVALID_INPUT')
        for key in ('compile_options', 'mount_options'):
            if type(value[key]) is not list or any(type(item) is not str for item in value[key]):
                raise ValueError('INVALID_INPUT')
        for key, length in [('evidence_id', 32), ('approved_policy_sha', 64)]:
            if type(value[key]) is not str or re.fullmatch('[0-9a-f]{%d}' % length, value[key]) is None:
                raise ValueError('INVALID_INPUT')
        return value
    if kind in ('active', 'anchor'):
        expected = ({'format_version', 'policy_id', 'deployment_policy_sha'} if kind == 'active'
                    else {'format_version', 'lock_path', 'lock_identity'})
        if (type(value) is not dict or set(value) != expected
                or type(value['format_version']) is not int or value['format_version'] != 1):
            raise ValueError('INVALID_INPUT')
        if kind == 'anchor':
            if value['lock_path'] != '/etc/hermes-display-bootstrap/bootstrap.lock':
                raise ValueError('INVALID_INPUT')
            validate_record(value['lock_identity'], 'file_identity')
        else:
            for key, length in [('policy_id', 32), ('deployment_policy_sha', 64)]:
                if type(value[key]) is not str or re.fullmatch('[0-9a-f]{%d}' % length, value[key]) is None:
                    raise ValueError('INVALID_INPUT')
        return value
    if kind == 'deployment':
        from pathlib import PurePosixPath
        expected = {'format_version', 'policy_id', 'creator_uid', 'approver_uid', 'roots',
                    'ancestors', 'lock_identity', 'platform_requirements', 'resource_policy_id',
                    'resource_policy_sha', 'expected_ddl_sha'}
        if (type(value) is not dict or set(value) != expected
                or type(value['format_version']) is not int or value['format_version'] != 1
                or any(type(value[k]) is not int for k in ('creator_uid', 'approver_uid'))
                or value['creator_uid'] == value['approver_uid']):
            raise ValueError('INVALID_INPUT')
        if value['expected_ddl_sha'] != '578f80789de98456324d430142631c7b7f984f80f3ffe30fc7c31d0634bdcd9d':
            raise ValueError('INVALID_INPUT')
        for key, length in [('policy_id', 32), ('resource_policy_id', 32), ('resource_policy_sha', 64)]:
            if type(value[key]) is not str or re.fullmatch('[0-9a-f]{%d}' % length, value[key]) is None:
                raise ValueError('INVALID_INPUT')
        root_keys = {'registry_root', 'approval_root', 'candidate_root', 'publish_root', 'lock_root'}
        if type(value['roots']) is not dict or set(value['roots']) != root_keys or type(value['ancestors']) is not list:
            raise ValueError('INVALID_INPUT')
        for item in list(value['roots'].values()) + value['ancestors']:
            if type(item) is not dict or set(item) != {'path', 'identity'}:
                raise ValueError('INVALID_INPUT')
            path = item['path']
            if (type(path) is not str or not path.startswith('/') or '\x00' in path
                    or any(p in ('.', '..') for p in path.split('/'))):
                raise ValueError('INVALID_INPUT')
            validate_record(item['identity'], 'directory_identity')
        paths = [item['path'] for item in value['ancestors']]
        required = {str(p) for item in value['roots'].values() for p in PurePosixPath(item['path']).parents}
        if paths != sorted(set(paths)) or set(paths) != required:
            raise ValueError('INVALID_INPUT')
        validate_record(value['lock_identity'], 'file_identity')
        platform = value['platform_requirements']
        platform_keys = {'kernel', 'sqlite_version', 'compile_options', 'vfs', 'filesystem', 'mount_id', 'mount_options', 'namespace_id'}
        if type(platform) is not dict or set(platform) != platform_keys:
            raise ValueError('INVALID_INPUT')
        validate_record(dict(platform, format_version=1, evidence_id='0'*32, approved_policy_sha='0'*64), 'platform_evidence')
        if any(platform[k] != sorted(set(platform[k])) for k in ('compile_options', 'mount_options')):
            raise ValueError('INVALID_INPUT')
        return value
    if kind != 'approval' or type(value) is not dict:
        raise ValueError('INVALID_INPUT')
    expected = {'format_version', 'approval_id', 'candidate_id', 'manifest_sha',
                'target_name', 'policy_sha', 'scope', 'approver_reference'}
    if (set(value) != expected or type(value['format_version']) is not int
            or value['format_version'] != 1):
        raise ValueError('INVALID_INPUT')
    for key, length in [('approval_id', 32), ('candidate_id', 32),
                        ('target_name', 32), ('manifest_sha', 64), ('policy_sha', 64)]:
        if type(value[key]) is not str or re.fullmatch('[0-9a-f]{%d}' % length, value[key]) is None:
            raise ValueError('INVALID_INPUT')
    if (value['scope'] != 'PUBLISH_EMPTY_UNACTIVATED'
            or type(value['approver_reference']) is not str):
        raise ValueError('INVALID_INPUT')
    return value


def _validate_v2(value, kind):
    """Reuse V1 shared fields without mutating or silently upgrading input."""
    extras = {
        'resource': {'audit_format', 'audit_slot_count'},
        'manifest': {'audit_format', 'audit_header_sha'},
        'deployment': {'storage_contract_id', 'storage_contract_sha', 'approval_policy_id',
                       'approval_policy_sha', 'creator_gid', 'approver_gid',
                       'approval_read_gid', 'boundary_probe_identity', 'boundary_probe_sha'},
    }[kind]
    if not extras <= value.keys():
        raise ValueError('INVALID_INPUT')
    shared = {k: v for k, v in value.items() if k not in extras}
    shared['format_version'] = 1
    validate_record(shared, kind)  # Unknown fields are still rejected here.
    if kind == 'resource':
        from api.display_bootstrap_audit_budget import validate_layout
        validate_layout(value)
    elif kind == 'manifest':
        if (value['audit_format'] != 'FIXED_LOG_V1'
                or type(value['audit_header_sha']) is not str
                or re.fullmatch('[0-9a-f]{64}', value['audit_header_sha']) is None):
            raise ValueError('INVALID_INPUT')
    else:
        for key in ('storage_contract_id', 'storage_contract_sha', 'approval_policy_id',
                    'approval_policy_sha', 'boundary_probe_sha'):
            size = 32 if key.endswith('_id') else 64
            if type(value[key]) is not str or re.fullmatch('[0-9a-f]{%d}' % size, value[key]) is None:
                raise ValueError('INVALID_INPUT')
        for key in ('creator_uid', 'approver_uid', 'creator_gid', 'approver_gid', 'approval_read_gid'):
            if type(value[key]) is not int or value[key] <= 0:
                raise ValueError('INVALID_INPUT')
        if (value['creator_gid'] == value['approver_gid']
                or value['approval_read_gid'] != value['creator_gid']):
            raise ValueError('INVALID_INPUT')
        probe = validate_record(value['boundary_probe_identity'], 'file_identity')
        import stat
        if probe['uid'] != value['creator_uid'] or stat.S_IMODE(probe['mode']) != 0o600:
            raise ValueError('INVALID_INPUT')
    return value


def _validate(value):
    kind = type(value)
    if value is None or kind is bool:
        return
    if kind is int and 0 <= value <= 9007199254740991:
        return
    if kind is str and 0 < len(value.encode('utf-8', 'strict')) <= 4096:
        return
    if kind is list and len(value) <= 256:
        for item in value:
            _validate(item)
        return
    if kind is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError('INVALID_INPUT')
            _validate(key)
            _validate(item)
        return
    raise ValueError('INVALID_INPUT')


def canonical_bytes(value):
    try:
        _validate(value)
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False, allow_nan=False).encode('utf-8', 'strict')
    except (RecursionError, UnicodeError) as exc:
        raise ValueError('INVALID_INPUT') from exc
    if len(raw) > 65536:
        raise ValueError('INVALID_INPUT')
    return raw


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('INVALID_INPUT')
        result[key] = value
    return result


def parse_record(raw):
    """Decode canonical JSON only; object-specific schemas are checked separately."""
    if type(raw) is not bytes or not 0 < len(raw) <= 65536:
        raise ValueError('INVALID_INPUT')
    try:
        value = json.loads(raw.decode('utf-8', 'strict'), object_pairs_hook=_unique_object)
        if type(value) is not dict:
            raise ValueError('INVALID_INPUT')
        if canonical_bytes(value) != raw:
            raise ValueError('NONCANONICAL_RECORD')
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('INVALID_INPUT') from exc
    return value


def digest(raw):
    return hashlib.sha256(raw).hexdigest()
