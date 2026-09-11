"""Explicit root quota broker entry point; no activation on import.

Start only after deployment approval. Existing sockets are never removed.
This broker does not activate databases or grant BootstrapContext admission.
"""
import os
import socket
from functools import partial
from pathlib import Path

from api import display_bootstrap_quota_config as settings
from api.display_bootstrap_quota_peer import authenticate_peer
from api.display_bootstrap_quota_server import QuotaHandler
from api.display_bootstrap_quota_kernel import verify_enforcement
from api.display_bootstrap_policy import (BootstrapRejected, open_protected_root,
    directory_identity, read_active_policy, read_resource_policy,
    open_policy_roots, recheck_policy_roots)


def serve():
    if os.geteuid() != 0:
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
    config, config_sha = settings.read()
    root = open_protected_root(settings.ROOT, {0})
    roots = {}
    ledger = device = parent = None
    listener = None
    try:
        deployment, deployment_sha = read_active_policy(root, config['policy_id'])
        if deployment_sha != config['policy_sha']:
            raise BootstrapRejected('APPROVAL_MISMATCH')
        resource = read_resource_policy(root, deployment)
        from api.display_bootstrap_runner_binding import resolve_runner_binding
        creator = resolve_runner_binding(deployment, resource, 'creator')
        if (creator.profile_id != config['hard_limit_profile_id']
                or resource['max_candidate_bytes'] != config['hard_bytes']
                or deployment['creator_uid'] != config['creator_uid']
                or deployment['creator_gid'] != config['creator_gid']):
            raise BootstrapRejected('APPROVAL_MISMATCH')
        def authenticate_request(pid, uid, gid, request):
            selected = resolve_runner_binding(deployment, resource, request['role'])
            if (request['deployment_sha'] != selected.deployment_sha
                    or request['resource_sha'] != selected.resource_sha
                    or request['runner_profile_id'] != selected.profile_id
                    or request['runner_profile_sha'] != selected.profile_sha):
                raise BootstrapRejected('APPROVAL_MISMATCH')
            evidence = authenticate_peer(pid, uid, gid, selected.profile_id)
            if evidence['profile_sha'] != selected.profile_sha:
                raise BootstrapRejected('IDENTITY_CHANGED')
            return evidence
        roots = open_policy_roots(deployment)
        ledger = open_protected_root(config['ledger_path'], {0})
        if directory_identity(ledger) != config['ledger_identity']:
            raise BootstrapRejected('IDENTITY_CHANGED')
        parent = open_protected_root(str(Path(config['device_path']).parent), {0})
        device = os.open(Path(config['device_path']).name,
                         os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        def revalidate():
            if resolve_runner_binding(deployment, resource, 'creator') != creator:
                raise BootstrapRejected('IDENTITY_CHANGED')
            if settings.read() != (config, config_sha):
                raise BootstrapRejected('IDENTITY_CHANGED')
            if read_active_policy(root, config['policy_id']) != (deployment, deployment_sha):
                raise BootstrapRejected('APPROVAL_MISMATCH')
            if read_resource_policy(root, deployment) != resource:
                raise BootstrapRejected('APPROVAL_MISMATCH')
            recheck_policy_roots(deployment, roots)
            fresh = open_protected_root(config['ledger_path'], {0})
            try:
                if directory_identity(fresh) != config['ledger_identity'] or directory_identity(ledger) != config['ledger_identity']:
                    raise BootstrapRejected('IDENTITY_CHANGED')
            finally:
                os.close(fresh)
            for info in (os.fstat(device), os.stat(config['device_path'], follow_symlinks=False)):
                if {key: getattr(info, 'st_' + key) for key in config['device_identity']} != config['device_identity']:
                    raise BootstrapRejected('IDENTITY_CHANGED')
            for key in ('candidate_root', 'publish_root'):
                if verify_enforcement(roots[key], device)['mount_id'] != config['mount_id']:
                    raise BootstrapRejected('IDENTITY_CHANGED')
        revalidate()
        handler = QuotaHandler(config=config, source_fd=roots['candidate_root'],
            publish_fd=roots['publish_root'], device_fd=device, ledger_fd=ledger,
            authenticate_peer=authenticate_request,
            validate_deployment=revalidate)
        from api.display_bootstrap_quota_socket import activated_listener
        listener, identity = activated_listener(root, config['creator_gid'])
        while True:
            revalidate()
            current = os.stat('quota.sock', dir_fd=root, follow_symlinks=False)
            if any(getattr(current, k) != getattr(identity, k) for k in
                   ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode')):
                raise BootstrapRejected('IDENTITY_CHANGED')
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            with connection:
                handler.handle(connection)
    finally:
        if listener is not None:
            listener.close()
        # Preserve endpoint and allocation evidence on stop/failure.
        for fd in (device, parent, ledger, *roots.values(), root):
            if fd is not None:
                os.close(fd)


if __name__ == '__main__':
    serve()
