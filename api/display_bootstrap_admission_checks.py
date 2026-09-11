"""Candidate-scoped confinement and revalidation shared by preparation/issued state."""
import os
from api.display_bootstrap_admission_ownership import _check_preparation
from api.display_bootstrap_policy import directory_identity, recheck_policy_roots, read_protected_record
from api.display_bootstrap_roles import verify_dac_configuration
from api.display_bootstrap_audit_log import _deadline
from api.display_bootstrap_preparation_steps import _quota_prepare, _scan_audit


def install(p):
    _check_preparation(p)
    from api.display_bootstrap_boundary_probe import probe
    from api.display_bootstrap_write_guard import install_fixed_boundary
    from api.display_bootstrap_audit_boundary import install_audit_termination_boundary
    probe(p.config,denied=False,deadline=p.deadline)
    if p.mode == 'CREATE':
        result = install_fixed_boundary(p.candidate_fd,p.audit_fd)
    elif p.mode == 'PUBLISH':
        result = install_fixed_boundary(p.roots['candidate_root'],p.audit_fd,
                                        publish_fd=p.roots['publish_root'])
    elif p.mode == 'AUDIT_TERMINATE':
        result = install_audit_termination_boundary(p.audit_fd)
    else:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    p.boundary = dict(pid=p.pid,candidate_id=p.candidate_id,
        registry_identity=directory_identity(p.audit_fd), installed=result,
        audit_identity={k:getattr(os.stat('audit.bin',dir_fd=p.audit_fd,follow_symlinks=False),'st_'+k)
                        for k in ('dev','ino','uid','gid','mode','nlink')})
    return p.boundary


def verify_common(*, config, resource, roots, storage, release, registry_fd, candidate_id,
                  boundary, role, deadline):
    _deadline(deadline)
    storage.revalidate()
    release.revalidate()
    if config != storage.snapshot('config') or resource != storage.snapshot('resource'):
        raise ValueError('IDENTITY_CHANGED')
    report = storage.snapshot('report')
    contract = storage.snapshot('contract')
    if (release.commit != report['code_commit'] or os.uname().release != report['kernel_release']
            or os.readlink('/proc/self/ns/mnt') != contract['namespace_id']):
        raise ValueError('APPROVAL_MISMATCH')
    from api.display_bootstrap_runner_binding import resolve_runner_binding, verify_runner_binding
    verify_runner_binding(resolve_runner_binding(config,resource,role),config,resource,deadline)
    from api.display_bootstrap_policy import verify_platform
    verify_platform(roots['candidate_root'],config['platform_requirements'])
    from api.display_bootstrap_storage_topology import observe
    current = observe(roots['registry_root'],deadline=deadline)
    topology = contract['audit_topology']
    if (any(current[key] != topology[key] for key in
            ('backing_identity','backing_bytes','host_disk_device','host_partition_start_sectors'))
            or current['volume']['mount_id'] != topology['loop_mount_id']
            or current['volume']['device'] != topology['loop_device']
            or current['host']['mount_id'] != topology['host_mount_id']
            or current['host']['device'] != topology['host_device']):
        raise ValueError('IDENTITY_CHANGED')
    verify_dac_configuration(roots,config,role=role,before_confinement=False)
    recheck_policy_roots(config,roots)
    if (boundary['pid'] != os.getpid() or boundary['candidate_id'] != candidate_id
            or boundary['registry_identity'] != directory_identity(registry_fd)):
        raise ValueError('IDENTITY_CHANGED')
    info = os.stat('audit.bin',dir_fd=registry_fd,follow_symlinks=False)
    if {k:getattr(info,'st_'+k) for k in boundary['audit_identity']} != boundary['audit_identity']:
        raise ValueError('IDENTITY_CHANGED')
    from api.display_bootstrap_seccomp import verify_quota_guard
    from api.display_bootstrap_boundary_probe import probe
    from api.display_bootstrap_volume import verify_fixed_volumes
    verify_quota_guard()
    probe(config,denied=True,deadline=deadline)
    verify_fixed_volumes(roots,resource,profile_id=storage.binding.volume_profile_id)
    _deadline(deadline)


def verify_prepared(p, baseline=None):
    _check_preparation(p)
    verify_common(config=p.config,resource=p.resource,roots=p.roots,storage=p.storage,
        release=p.release,registry_fd=p.audit_fd,candidate_id=p.candidate_id,boundary=p.boundary,
        role=p.role,deadline=p.deadline)
    if p.mode in ('CREATE','PUBLISH'):
        _quota_prepare(p)
    if p.mode == 'PUBLISH':
        raw = read_protected_record(p.roots['approval_root'],p.approval_id+'.json',
                                    {p.config['approver_uid']})
        if raw != p.approval_raw:
            raise ValueError('IDENTITY_CHANGED')
    if _scan_audit(p) != p.audit_snapshot:
        raise ValueError('IDENTITY_CHANGED')
    verified = (p.pid,p.role,p.mode,p.candidate_id,p.deployment_sha,p.storage.binding,
                p.release.commit,p.deadline,tuple(sorted(p.boundary['audit_identity'].items())))
    if baseline is not None and verified != baseline:
        raise ValueError('IDENTITY_CHANGED')
    return verified
