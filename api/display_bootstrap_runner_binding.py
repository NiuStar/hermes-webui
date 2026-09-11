"""Immutable role selection; selection is not runtime admission."""
from dataclasses import dataclass
from api.display_bootstrap_manifest import canonical_bytes, digest, validate_record
from api.display_bootstrap_runner import read_hard_limit_profile, verify_systemd_runner
from api.display_bootstrap_v3 import ROLES
from api.display_bootstrap_audit_log import _deadline


@dataclass(frozen=True)
class RunnerBinding:
    role: str
    profile_id: str
    profile_sha: str
    resource_sha: str
    deployment_sha: str

    def actor(self):
        return dict(role=self.role, profile_id=self.profile_id, profile_sha=self.profile_sha)


def resolve_runner_binding(config, resource, role):
    validate_record(config, 'deployment')
    validate_record(resource, 'resource')
    if (config['format_version'] != 3 or resource['format_version'] != 3
            or role not in ROLES or digest(canonical_bytes(resource)) != config['resource_policy_sha']):
        raise ValueError('APPROVAL_MISMATCH')
    units = set()
    for expected_role in ROLES:
        entry = resource['hard_limit_profiles'][expected_role]
        profile, sha = read_hard_limit_profile(entry['profile_id'])
        approver = expected_role == 'approver'
        uid = config['approver_uid' if approver else 'creator_uid']
        gid = config['approver_gid' if approver else 'creator_gid']
        groups = [config['approval_read_gid']] if approver else []
        if (sha != entry['profile_sha'] or profile['format_version'] != 2
                or profile['role'] != expected_role or profile['uid'] != uid
                or profile['gid'] != gid or profile['supplementary_gids'] != groups
                or profile['unit'] in units):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        if (profile['memory_max_bytes'] > resource['max_rss_bytes']
                or profile['runtime_max_usec'] > resource['max_elapsed_seconds'] * 1000000):
            raise ValueError('RESOURCE_LIMIT')
        units.add(profile['unit'])
    entry = resource['hard_limit_profiles'][role]
    return RunnerBinding(role, entry['profile_id'], entry['profile_sha'],
                         config['resource_policy_sha'], digest(canonical_bytes(config)))


def verify_runner_binding(binding, config, resource, deadline):
    _deadline(deadline)
    if type(binding) is not RunnerBinding or resolve_runner_binding(config, resource, binding.role) != binding:
        raise ValueError('IDENTITY_CHANGED')
    evidence = verify_systemd_runner(binding.profile_id,
        max_rss_bytes=resource['max_rss_bytes'], max_elapsed_seconds=resource['max_elapsed_seconds'],
        config=config, role=binding.role)
    _deadline(deadline)
    if evidence['profile_sha'] != binding.profile_sha:
        raise ValueError('IDENTITY_CHANGED')
    if resolve_runner_binding(config, resource, binding.role) != binding:
        raise ValueError('IDENTITY_CHANGED')
    return dict(evidence, role=binding.role, profile_id=binding.profile_id,
                resource_sha=binding.resource_sha, deployment_sha=binding.deployment_sha)
