"""Fixed-path credential handshake client; never accepts arbitrary endpoints."""
import os
import socket
import stat
from api.display_bootstrap_policy import open_protected_root
from api.display_bootstrap_audit_log import _deadline
from api.display_bootstrap_storage_handshake import client,validate_binding
from api.display_bootstrap_storage_peer import hold_observer


def request(storage_handle, *, role, registry_fd, deadline):
    from contextlib import contextmanager
    from api.display_bootstrap_runner_binding import resolve_runner_binding
    from api.display_bootstrap_storage_observation import validate_storage_observation
    from api.display_bootstrap_observer_release import read_observer
    from api.display_bootstrap_storage_topology import observe
    storage_handle.revalidate()
    config = storage_handle.snapshot('config')
    resource = storage_handle.snapshot('resource')
    selected = resolve_runner_binding(config, resource, role)
    fixed = storage_handle.binding
    binding = dict(deployment_sha=fixed.deployment_sha, resource_sha=fixed.resource_sha,
                   role=role, runner_profile_id=selected.profile_id,
                   runner_profile_sha=selected.profile_sha,
                   volume_profile_id=fixed.volume_profile_id, volume_profile_sha=fixed.volume_profile_sha,
                   observer_profile_id=config['observer_profile_id'],
                   observer_profile_sha=config['observer_profile_sha'])
    evidence = {}
    accepted = {}
    @contextmanager
    def authenticated_observer(peer, binding, deadline):
        with hold_observer(peer, binding, deadline) as verify:
            profile, _ = read_observer(binding['observer_profile_id'], binding['observer_profile_sha'], deadline)
            evidence['profile'] = profile
            def final_verify():
                storage_handle.revalidate()
                if 'result' in accepted:
                    validate_result(accepted['result'])
                verify()
            yield final_verify
    def validate_result(result):
        local = observe(registry_fd, deadline=deadline)
        topology = storage_handle.snapshot('contract')['audit_topology']
        for key in ('backing_identity', 'backing_bytes', 'host_disk_device', 'host_partition_start_sectors'):
            if local[key] != topology[key]:
                raise ValueError('IDENTITY_CHANGED')
        validate_storage_observation(result, storage_handle=storage_handle,
            observer_evidence=evidence, local_evidence=dict(loop=local['volume'], host=local['host']),
            deadline=deadline)
        accepted['result'] = result
    validate_binding(binding)
    _deadline(deadline)
    root=open_protected_root('/run/hermes-display-bootstrap',{0})
    try:
        before=os.stat('storage.sock',dir_fd=root,follow_symlinks=False)
        if (not stat.S_ISSOCK(before.st_mode) or before.st_uid!=0
                or before.st_gid!=config['creator_gid'] or stat.S_IMODE(before.st_mode)!=0o660):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        fields=('st_dev','st_ino','st_uid','st_gid','st_mode')
        def unchanged():
            now=os.stat('storage.sock',dir_fd=root,follow_symlinks=False)
            if any(getattr(now,k)!=getattr(before,k) for k in fields):
                raise ValueError('IDENTITY_CHANGED')
            _deadline(deadline)
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
            import time
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_PASSCRED,1)
            sock.settimeout(max(0.001,deadline-time.monotonic()))
            sock.connect('/proc/self/fd/%d/storage.sock'%root)
            unchanged()
            result=client(sock,binding,deadline=deadline,hold_observer=authenticated_observer,
                          validate_observation=validate_result)
            unchanged()
            return result
    finally:
        os.close(root)
