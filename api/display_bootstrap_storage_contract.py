"""Storage contract codec: assertions are never live observation evidence."""
import re

from api.display_bootstrap_manifest import canonical_bytes, validate_record


def _object(value, fields):
    if type(value) is not dict or set(value) != set(fields.split()):
        raise ValueError('INVALID_INPUT')


def _hex(value, size):
    if type(value) is not str or re.fullmatch('[0-9a-f]{%d}' % size, value) is None:
        raise ValueError('INVALID_INPUT')


def _integer(value, minimum=1):
    if type(value) is not int or not minimum <= value <= 9007199254740991:
        raise ValueError('INVALID_INPUT')


def validate_contract(value):
    canonical_bytes(value)
    if type(value) is not dict:
        raise ValueError('INVALID_INPUT')
    version = value.get('format_version')
    if type(version) is not int or version not in (1, 2):
        raise ValueError('INVALID_INPUT')
    fields = ('format_version storage_contract_id volume_profile_sha namespace_id '
              'audit_topology administrator_assurance acceptance')
    _object(value, fields + (' volume_profile_id' if version == 2 else ''))
    if version == 2:
        _hex(value['volume_profile_id'], 32)
    _hex(value['storage_contract_id'], 32)
    _hex(value['volume_profile_sha'], 64)
    if type(value['namespace_id']) is not str or re.fullmatch(r'mnt:\[[0-9]+\]', value['namespace_id']) is None:
        raise ValueError('INVALID_INPUT')
    topology = value['audit_topology']
    _object(topology, 'loop_device loop_mount_id loop_fs_uuid backing_identity backing_bytes '
                     'host_mount_id host_device host_fs_uuid host_disk_device '
                     'host_partition_start_sectors block_bytes required_mount_options')
    for key in ('loop_device', 'host_device', 'host_disk_device'):
        _object(topology[key], 'major minor')
        for number in topology[key].values():
            _integer(number, 0)
    for key in ('loop_mount_id', 'backing_bytes', 'host_mount_id', 'block_bytes'):
        _integer(topology[key])
    _integer(topology['host_partition_start_sectors'], 0)
    if topology['block_bytes'] != 4096:
        raise ValueError('UNSUPPORTED_PLATFORM')
    for key in ('loop_fs_uuid', 'host_fs_uuid'):
        if type(topology[key]) is not str or re.fullmatch(
                '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', topology[key]) is None:
            raise ValueError('INVALID_INPUT')
    validate_record(topology['backing_identity'], 'file_identity')
    options = topology['required_mount_options']
    if (type(options) is not list or any(type(k) is not str for k in options)
            or options != sorted(set(options)) or 'rw' not in options or 'ro' in options):
        raise ValueError('INVALID_INPUT')
    assurance = value['administrator_assurance']
    _object(assurance, 'deployment_reference responsible_party no_overcommit '
                       'no_snapshot_rollback flush_honored unobservable_layers')
    for key in ('no_overcommit', 'no_snapshot_rollback', 'flush_honored'):
        if assurance[key] is not True:
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    for key in ('deployment_reference', 'responsible_party'):
        if type(assurance[key]) is not str or not assurance[key].strip():
            raise ValueError('INVALID_INPUT')
    layers = assurance['unobservable_layers']
    if type(layers) is not list or not layers or any(type(k) is not str or not k.strip() for k in layers):
        raise ValueError('INVALID_INPUT')
    acceptance = value['acceptance']
    _object(acceptance, 'code_commit kernel_release volume_profile_sha report_sha report_id')
    for key, size in [('report_id', 32), ('volume_profile_sha', 64), ('report_sha', 64)]:
        _hex(acceptance[key], size)
    if (type(acceptance['code_commit']) is not str or re.fullmatch(
            '(?:[0-9a-f]{40}|[0-9a-f]{64})', acceptance['code_commit']) is None
            or type(acceptance['kernel_release']) is not str or not acceptance['kernel_release']
            or acceptance['volume_profile_sha'] != value['volume_profile_sha']):
        raise ValueError('INVALID_INPUT')
    return value


PROOF_SCOPE = {
    'format_version': 1, 'scope': 'APPLICATION_MANAGED_GROWTH',
    'excluded_writers': ['UNRELATED_HOST_PROCESSES'],
    'application_journal_policy': 'NO_BUSINESS_LOGS_BOUNDED_LIFECYCLE_EVENTS',
    'host_headroom_required': True,
}
EVIDENCE_KINDS = {
    'capacity_headroom': 'live_observation', 'bounded_growth': 'normal_workload',
    'early_rejection': 'controlled_injection',
    'lifecycle_failure_close': 'normal_workload',
    'crash_recovery': 'controlled_injection', 'sync_error': 'controlled_injection',
    'real_role_channels': 'normal_workload',
}


def validate_acceptance(value, *, admission=False):
    canonical_bytes(value)
    if type(value) is not dict or type(value.get('format_version')) is not int:
        raise ValueError('INVALID_INPUT')
    if value['format_version'] == 1:
        if admission:
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        return _validate_legacy_acceptance(value)
    if value['format_version'] != 2:
        raise ValueError('INVALID_INPUT')
    _object(value, 'format_version report_id code_commit kernel_release volume_profile_sha '
                   'topology_sha checks proof_scope')
    # Canonical comparison distinguishes true from integer 1 throughout the scope.
    if canonical_bytes(value['proof_scope']) != canonical_bytes(PROOF_SCOPE):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    for key, size in [('report_id', 32), ('volume_profile_sha', 64), ('topology_sha', 64)]:
        _hex(value[key], size)
    if (type(value['code_commit']) is not str or re.fullmatch(
            '(?:[0-9a-f]{40}|[0-9a-f]{64})', value['code_commit']) is None
            or type(value['kernel_release']) is not str or not value['kernel_release']):
        raise ValueError('INVALID_INPUT')
    _object(value['checks'], ' '.join(EVIDENCE_KINDS))
    for name, kind in EVIDENCE_KINDS.items():
        check = value['checks'][name]
        _object(check, 'status evidence_sha evidence_kind')
        if check['status'] != 'PASS' or check['evidence_kind'] != kind:
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        _hex(check['evidence_sha'], 64)
    return value


def _validate_legacy_acceptance(value):
    canonical_bytes(value)
    _object(value, 'format_version report_id code_commit kernel_release volume_profile_sha topology_sha checks')
    if type(value['format_version']) is not int or value['format_version'] != 1:
        raise ValueError('INVALID_INPUT')
    for key, size in [('report_id', 32), ('volume_profile_sha', 64), ('topology_sha', 64)]:
        _hex(value[key], size)
    if (type(value['code_commit']) is not str or re.fullmatch(
            '(?:[0-9a-f]{40}|[0-9a-f]{64})', value['code_commit']) is None
            or type(value['kernel_release']) is not str or not value['kernel_release']):
        raise ValueError('INVALID_INPUT')
    _object(value['checks'], 'audit_blocks_full audit_inodes_full host_blocks_full '
                            'lifecycle_failure_close crash_recovery sync_error real_role_channels')
    for check in value['checks'].values():
        _object(check, 'status evidence_sha')
        if check['status'] != 'PASS':
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        _hex(check['evidence_sha'], 64)
    return value
