"""Protected storage records and comparison; no cached-observation fallback."""
import os

from api.display_bootstrap_manifest import parse_record, digest, canonical_bytes
from api.display_bootstrap_storage_contract import validate_contract, validate_acceptance
from api.display_bootstrap_storage_topology import observe
from api.display_bootstrap_audit_log import _deadline


def _read(directory, record_id):
    from api.display_bootstrap_policy import open_protected_root, read_protected_record
    from api.display_bootstrap_storage_contract import _hex
    _hex(record_id, 32)
    fd = open_protected_root(directory, {0})
    try:
        raw = read_protected_record(fd, record_id + '.json', {0})
        return parse_record(raw), digest(raw)
    finally:
        os.close(fd)


def verify(config, registry_fd, volume_profile_sha, *, deadline, code_commit, journal_observation):
    """Compare fresh observed topology and trusted fixed acceptance inputs.

    journal_observation must come from the verified root peer, not caller JSON.
    This low-level comparison is intentionally not an admission entrypoint.
    """
    _deadline(deadline)
    contract, sha = _read('/etc/hermes-display-bootstrap/storage-contracts', config['storage_contract_id'])
    validate_contract(contract)
    if sha != config['storage_contract_sha'] or contract['volume_profile_sha'] != volume_profile_sha:
        raise ValueError('APPROVAL_MISMATCH')
    acceptance = contract['acceptance']
    report, report_sha = _read('/etc/hermes-display-bootstrap/storage-acceptance', acceptance['report_id'])
    validate_acceptance(report, admission=True)
    if (report_sha != acceptance['report_sha'] or code_commit != acceptance['code_commit']
            or any(report[k] != acceptance[k] for k in
                   ('report_id', 'code_commit', 'kernel_release', 'volume_profile_sha'))
            or os.uname().release != acceptance['kernel_release']
            or os.readlink('/proc/self/ns/mnt') != contract['namespace_id']):
        raise ValueError('APPROVAL_MISMATCH')
    current = observe(registry_fd, deadline=deadline)
    topology = contract['audit_topology']
    actual = dict(loop_device=current['volume']['device'],
                  loop_mount_id=current['volume']['mount_id'],
                  backing_identity=current['backing_identity'], backing_bytes=current['backing_bytes'],
                  host_mount_id=current['host']['mount_id'], host_device=current['host']['device'],
                  host_disk_device=current['host_disk_device'],
                  host_partition_start_sectors=current['host_partition_start_sectors'],
                  required_mount_options=current['volume']['options'])
    if any(topology[k] != v for k, v in actual.items()):
        raise ValueError('IDENTITY_CHANGED')
    for key, device, uuid in [('loop', topology['loop_device'], topology['loop_fs_uuid']),
                              ('host', topology['host_device'], topology['host_fs_uuid'])]:
        item = journal_observation[key]
        if (item['device'] != device or item['fs_uuid'] != uuid or item['block_bytes'] != 4096
                or item['internal_journal'] is not True or item['errors'] != 0):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    if report['topology_sha'] != digest(canonical_bytes(topology)):
        raise ValueError('APPROVAL_MISMATCH')
    if _read('/etc/hermes-display-bootstrap/storage-contracts', config['storage_contract_id']) != (contract, sha):
        raise ValueError('IDENTITY_CHANGED')
    if _read('/etc/hermes-display-bootstrap/storage-acceptance', acceptance['report_id']) != (report, report_sha):
        raise ValueError('IDENTITY_CHANGED')
    _deadline(deadline)
    return dict(contract_sha=sha, report_sha=report_sha, observed=current,
                administrator_assurance=contract['administrator_assurance'],
                acceptance=report['checks'], journal=journal_observation)
