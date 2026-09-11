"""Held fixed-path V2 storage authority; never selects a caller supplied volume."""
import copy
import os
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

from api.display_bootstrap_audit_log import _deadline
from api.display_bootstrap_manifest import parse_record, validate_record, digest, canonical_bytes
from api.display_bootstrap_policy import (open_protected_root, read_protected_record,
    directory_identity, verify_anchor_binding)
from api.display_bootstrap_storage_contract import validate_contract, validate_acceptance, _hex
from api.display_bootstrap_volume import validate_volume_profile

ROOT = '/etc/hermes-display-bootstrap'
FILE_FIELDS = ('dev','ino','uid','gid','mode','nlink','size','mtime_ns','ctime_ns')


@dataclass(frozen=True)
class StorageBinding:
    deployment_sha: str
    resource_sha: str
    storage_contract_id: str
    storage_contract_sha: str
    volume_profile_id: str
    volume_profile_sha: str


class StorageBindingHandle:
    def __init__(self, policy_id, deadline):
        _hex(policy_id, 32)
        self._policy_id, self._deadline = policy_id, deadline
        self._stack = ExitStack()
        self._directories, self._records = {}, {}
        self._closed = False
        try:
            self._load()
            self.revalidate()
        except BaseException:
            self.close()
            raise

    def _directory(self, relative):
        _deadline(self._deadline)
        if relative not in self._directories:
            path = ROOT + ('/' + relative if relative else '')
            fd = open_protected_root(path, {0})
            self._stack.callback(os.close, fd)
            self._directories[relative] = (fd, directory_identity(fd))
        return self._directories[relative][0]

    def _read(self, relative, name):
        fd = self._directory(relative)
        def identity():
            item = os.stat(name, dir_fd=fd, follow_symlinks=False)
            return tuple(getattr(item, 'st_' + key) for key in FILE_FIELDS)
        before = identity()
        raw = read_protected_record(fd, name, {0})
        if before != identity():
            raise ValueError('IDENTITY_CHANGED')
        snapshot = before, raw
        key = relative, name
        if key in self._records and self._records[key] != snapshot:
            raise ValueError('IDENTITY_CHANGED')
        self._records[key] = snapshot
        _deadline(self._deadline)
        return parse_record(raw), digest(raw)

    def _load(self):
        active, _ = self._read('', 'active.json')
        validate_record(active, 'active')
        if active['policy_id'] != self._policy_id:
            raise ValueError('APPROVAL_MISMATCH')
        config, deployment_sha = self._read('policies', self._policy_id + '.json')
        validate_record(config, 'deployment')
        if config['format_version'] != 3 or deployment_sha != active['deployment_policy_sha']:
            raise ValueError('APPROVAL_MISMATCH')
        anchor, _ = self._read('', 'anchor.json')
        validate_record(anchor, 'anchor')
        verify_anchor_binding(self._directory(''), anchor, config)
        resource, resource_sha = self._read('policies/resources', config['resource_policy_id'] + '.json')
        validate_record(resource, 'resource')
        if resource['format_version'] != 3 or resource_sha != config['resource_policy_sha']:
            raise ValueError('APPROVAL_MISMATCH')
        contract, contract_sha = self._read('storage-contracts', config['storage_contract_id'] + '.json')
        validate_contract(contract)
        if (contract['format_version'] != 2 or contract_sha != config['storage_contract_sha']
                or contract['storage_contract_id'] != config['storage_contract_id']):
            raise ValueError('APPROVAL_MISMATCH')
        volume_id = contract['volume_profile_id']
        volume, volume_sha = self._read('volumes', volume_id + '.json')
        validate_volume_profile(volume, volume_id)
        if volume['hard_limit_profile_id'] != volume_id or volume_sha != contract['volume_profile_sha']:
            raise ValueError('APPROVAL_MISMATCH')
        acceptance = contract['acceptance']
        report, report_sha = self._read('storage-acceptance', acceptance['report_id'] + '.json')
        validate_acceptance(report, admission=True)
        if (report_sha != acceptance['report_sha'] or any(report[k] != acceptance[k] for k in
                ('report_id','code_commit','kernel_release','volume_profile_sha'))
                or report['topology_sha'] != digest(canonical_bytes(contract['audit_topology']))):
            raise ValueError('APPROVAL_MISMATCH')
        self._binding = StorageBinding(deployment_sha, resource_sha, contract['storage_contract_id'],
                                       contract_sha, volume_id, volume_sha)
        self._values = dict(config=config, resource=resource, contract=contract, volume=volume, report=report)

    @property
    def binding(self):
        if self._closed:
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        return self._binding

    def snapshot(self, name):
        if self._closed:
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        return copy.deepcopy(self._values[name])

    def revalidate(self):
        if self._closed:
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        try:
            for relative, (held, expected) in self._directories.items():
                _deadline(self._deadline)
                path = ROOT + ('/' + relative if relative else '')
                fresh = open_protected_root(path, {0})
                try:
                    if directory_identity(held) != expected or directory_identity(fresh) != expected:
                        raise ValueError('IDENTITY_CHANGED')
                finally:
                    os.close(fresh)
            previous = self._binding
            self._load()
            if self._binding != previous:
                raise ValueError('IDENTITY_CHANGED')
        except BaseException:
            self.close()
            raise

    def close(self):
        self._closed = True
        self._stack.close()


@contextmanager
def hold_storage_binding(policy_id, *, deadline):
    handle = StorageBindingHandle(policy_id, deadline)
    try:
        yield handle
    finally:
        handle.close()
