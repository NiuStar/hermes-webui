"""Real worker credentials bound to active V3 policy, not caller assertions."""
import os
from contextlib import contextmanager
from api.display_bootstrap_policy import open_protected_root,read_protected_record,read_active_policy,read_resource_policy
from api.display_bootstrap_manifest import parse_record,validate_record
from api.display_bootstrap_runner_binding import resolve_runner_binding
from api.display_bootstrap_quota_peer import hold_peer,authenticate_peer
from api.display_bootstrap_audit_log import _deadline


def verify_worker_binding(peer,binding,deadline):
    _deadline(deadline)
    pid,uid,gid=peer
    root=open_protected_root('/etc/hermes-display-bootstrap',{0})
    try:
        active=validate_record(parse_record(read_protected_record(root,'active.json',{0})),'active')
        config,sha=read_active_policy(root,active['policy_id'])
        resource=read_resource_policy(root,config)
        selected=resolve_runner_binding(config,resource,binding['role'])
        if (sha!=binding['deployment_sha'] or selected.resource_sha!=binding['resource_sha']
                or selected.profile_id!=binding['runner_profile_id']
                or selected.profile_sha!=binding['runner_profile_sha']
                or config['observer_profile_id']!=binding['observer_profile_id']
                or config['observer_profile_sha']!=binding['observer_profile_sha']):
            raise ValueError('APPROVAL_MISMATCH')
        from api.display_bootstrap_storage_binding import hold_storage_binding
        with hold_storage_binding(active['policy_id'], deadline=deadline) as storage:
            fixed = storage.binding
            if (fixed.deployment_sha != sha or fixed.resource_sha != binding['resource_sha']
                    or fixed.volume_profile_id != binding['volume_profile_id']
                    or fixed.volume_profile_sha != binding['volume_profile_sha']):
                raise ValueError('APPROVAL_MISMATCH')
            storage.revalidate()
        evidence=authenticate_peer(pid,uid,gid,selected.profile_id)
        if evidence['profile_sha']!=selected.profile_sha:
            raise ValueError('IDENTITY_CHANGED')
        if read_active_policy(root,active['policy_id'])!=(config,sha) or read_resource_policy(root,config)!=resource:
            raise ValueError('IDENTITY_CHANGED')
        _deadline(deadline)
        return config,resource,evidence
    finally:
        os.close(root)


@contextmanager
def hold_worker(peer,binding,deadline):
    if binding['role'] not in ('creator','publisher','recover'):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    with hold_peer(peer[0]) as alive:
        baseline=verify_worker_binding(peer,binding,deadline)
        from api.display_bootstrap_storage_binding import hold_storage_binding
        root = open_protected_root('/etc/hermes-display-bootstrap', {0})
        try:
            active = validate_record(parse_record(read_protected_record(root, 'active.json', {0})), 'active')
        finally:
            os.close(root)
        with hold_storage_binding(active['policy_id'], deadline=deadline) as storage:
            if (storage.binding.deployment_sha != binding['deployment_sha']
                    or storage.binding.volume_profile_id != binding['volume_profile_id']
                    or storage.binding.volume_profile_sha != binding['volume_profile_sha']):
                raise ValueError('APPROVAL_MISMATCH')
            def verify():
                alive()
                storage.revalidate()
                if verify_worker_binding(peer,binding,deadline)!=baseline:
                    raise ValueError('IDENTITY_CHANGED')
                alive()
            verify()
            yield verify
            verify()
