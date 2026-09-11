"""Protected quota broker client. No local privilege or unlimited fallback.

The root-owned broker must validate peer credentials, deployment policy and
held directory identity, allocate once, and query the kernel on every verify.
No broker is started by this module. Absent broker means blocked creation.
"""
import array
import json
import os
import socket
import stat
import struct
from api.display_bootstrap_policy import BootstrapRejected, open_protected_root, directory_identity
from api.display_bootstrap_manifest import canonical_bytes, parse_record

ROOT = '/etc/hermes-display-bootstrap'


def candidate_quota(ctx, cid, fd, *, allocate=False):
    ctx.check_owner()
    return _candidate_quota_transport(config=ctx.config, resource=ctx.resource,
        role=ctx.role, deployment_sha=ctx.deployment_sha, deadline=ctx.operation_deadline(),
        cid=cid, fd=fd, allocate=allocate)


def _candidate_quota_transport(*, config, resource, role, deployment_sha, deadline, cid, fd, allocate=False):
    import re
    if type(cid) is not str or re.fullmatch('[0-9a-f]{32}', cid) is None:
        raise BootstrapRejected('INVALID_INPUT')
    from api.display_bootstrap_seccomp import verify_quota_guard
    try:
        verify_quota_guard()
    except (RuntimeError, OSError) as exc:
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN') from exc
    root = open_protected_root(ROOT, {0})
    try:
        named = os.stat('quota.sock', dir_fd=root, follow_symlinks=False)
        if not stat.S_ISSOCK(named.st_mode) or named.st_uid != 0 or named.st_mode & 0o007:
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        identity = directory_identity(fd)
        from api.display_bootstrap_runner_binding import resolve_runner_binding
        from api.display_bootstrap_quota_protocol import validate_request, BINDING
        selected = resolve_runner_binding(config, resource, role)
        binding = dict(deployment_sha=deployment_sha, resource_sha=selected.resource_sha,
                       role=selected.role, runner_profile_id=selected.profile_id, runner_profile_sha=selected.profile_sha)
        request = validate_request(dict(binding, version=2, operation='allocate' if allocate else 'verify',
                       candidate_id=cid, directory_identity=identity, max_bytes=resource['max_candidate_bytes']))
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
            import time
            from api.display_bootstrap_audit_log import _deadline
            def remaining():
                _deadline(deadline)
                connection.settimeout(min(5, max(0.001, deadline - time.monotonic())))
            remaining()
            connection.connect(f'/proc/self/fd/{root}/quota.sock')
            _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != 0:
                raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
            remaining()
            raw = canonical_bytes(request)
            sent = connection.sendmsg([raw], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [fd]))])
            if sent != len(raw):
                raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
            remaining()
            raw, ancillary, flags, _ = connection.recvmsg(
                65537, socket.CMSG_SPACE(16 * array.array('i').itemsize), socket.MSG_CMSG_CLOEXEC)
            for level, kind, data in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    received = array.array('i')
                    received.frombytes(data[:len(data) - len(data) % received.itemsize])
                    for extra_fd in received:
                        os.close(extra_fd)
            if ancillary or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(raw) > 65536:
                raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        response = parse_record(raw)
        if type(response) is dict and set(response) == {'version', 'error'}:
            if (type(response['version']) is int and response['version'] == 2
                    and type(response['error']) is str and response['error'] in (
                        'ACCESS_BOUNDARY_UNPROVEN', 'IDENTITY_CHANGED', 'STATE_CONFLICT',
                        'RESOURCE_LIMIT', 'LOCK_BUSY', 'INVALID_INPUT',
                        'APPROVAL_MISMATCH', 'UNSUPPORTED_PLATFORM')):
                raise BootstrapRejected(response['error'])
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        _deadline(deadline)
        fields = BINDING | {'version', 'candidate_id', 'directory_identity',
                  'project_id', 'hard_bytes', 'used_bytes', 'inherit', 'enforced'}
        if type(response) is not dict or set(response) != fields:
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        if (type(response['version']) is not int or response['version'] != 2
                or response['candidate_id'] != cid or any(response[k] != binding[k] for k in BINDING)
                or response['directory_identity'] != identity or directory_identity(fd) != identity
                or response['inherit'] is not True or response['enforced'] is not True
                or type(response['project_id']) is not int or not 0 < response['project_id'] < 2**31
                or type(response['hard_bytes']) is not int
                or response['hard_bytes'] != resource['max_candidate_bytes']
                or type(response['used_bytes']) is not int or response['used_bytes'] < 0):
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        if response['used_bytes'] > response['hard_bytes']:
            raise BootstrapRejected('RESOURCE_LIMIT')
        # Independently read project identity from held inode, never just receipt.
        import fcntl
        data = bytearray(28)
        fcntl.ioctl(fd, 0x801c581f, data, True)
        xflags, _, _, project, _ = struct.unpack('=IIIII8x', data)
        if project != response['project_id'] or not xflags & 0x200:
            raise BootstrapRejected('IDENTITY_CHANGED')
        return response
    except BootstrapRejected:
        raise
    except (OSError, ValueError) as exc:
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN') from exc
    finally:
        os.close(root)
