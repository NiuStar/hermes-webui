"""Privileged quota request handler. No daemon or service activation on import.

Receives exactly one held directory. Configuration is supplied only by a
root-owned launcher after active-policy validation; not from the request.
The launcher must also enforce the approved systemd peer and mount boundary.
"""
import array
import os
import socket
import stat
import struct

from api.display_bootstrap_manifest import canonical_bytes, parse_record
from api.display_bootstrap_policy import BootstrapRejected, directory_identity
from api import display_bootstrap_quota_kernel as kernel
from api import display_bootstrap_quota_ledger as ledger


class QuotaHandler:
    def __init__(self, *, config, source_fd, publish_fd, device_fd, ledger_fd,
                 authenticate_peer, validate_deployment):
        if os.geteuid() != 0:
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        self.config = config
        self.source_fd, self.publish_fd = source_fd, publish_fd
        self.device_fd, self.ledger_fd = device_fd, ledger_fd
        self.authenticate_peer = authenticate_peer
        self.validate_deployment = validate_deployment

    def handle(self, connection):
        received = []
        from contextlib import ExitStack
        from api.display_bootstrap_quota_peer import hold_peer
        lifetime = ExitStack()
        try:
            connection.settimeout(5)
            pid, uid, gid = struct.unpack('3i', connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != self.config['creator_uid'] or gid != self.config['creator_gid']:
                raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
            alive = lifetime.enter_context(hold_peer(pid))
            peer_identity = None
            self.validate_deployment()
            raw, ancillary, flags, _ = connection.recvmsg(
                65537, socket.CMSG_SPACE(16 * array.array('i').itemsize),
                socket.MSG_CMSG_CLOEXEC)
            invalid_ancillary = False
            for level, kind, data in ancillary:
                if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                    invalid_ancillary = True
                    continue
                fds = array.array('i')
                if len(data) % fds.itemsize:
                    invalid_ancillary = True
                fds.frombytes(data[:len(data) - len(data) % fds.itemsize])
                received.extend(fds)
            if invalid_ancillary or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(raw) > 65536 or len(received) != 1:
                raise BootstrapRejected('INVALID_INPUT')
            from api.display_bootstrap_quota_protocol import validate_request
            request = validate_request(parse_record(raw))
            peer_identity = self.authenticate_peer(pid, uid, gid, request)
            if self.authenticate_peer(pid, uid, gid, request) != peer_identity:
                raise BootstrapRejected('IDENTITY_CHANGED')
            alive()
            def check_authenticated_peer():
                alive()
                if self.authenticate_peer(pid, uid, gid, request) != peer_identity:
                    raise BootstrapRejected('IDENTITY_CHANGED')
            response = self.process(request, received[0], check_peer=check_authenticated_peer)
            alive()
            if self.authenticate_peer(pid, uid, gid, request) != peer_identity:
                raise BootstrapRejected('IDENTITY_CHANGED')
            self.validate_deployment()
            payload = canonical_bytes(response)
            if connection.send(payload) != len(payload):
                raise OSError('short quota response')
        except (BootstrapRejected, OSError, ValueError) as exc:
            # Return only stable, allowlisted error codes, never OS messages.
            code = exc.code if isinstance(exc, BootstrapRejected) else 'ACCESS_BOUNDARY_UNPROVEN'
            if code not in ('ACCESS_BOUNDARY_UNPROVEN', 'IDENTITY_CHANGED',
                            'STATE_CONFLICT', 'RESOURCE_LIMIT', 'LOCK_BUSY',
                            'INVALID_INPUT', 'APPROVAL_MISMATCH', 'UNSUPPORTED_PLATFORM'):
                code = 'ACCESS_BOUNDARY_UNPROVEN'
            try:
                connection.send(canonical_bytes({'version': 2, 'error': code}))
            except OSError:
                pass
        finally:
            lifetime.close()
            for fd in received:
                os.close(fd)

    def process(self, request, fd, *, check_peer):
        """Serialize across threads/processes using a fresh open description."""
        import fcntl
        lock = os.open('.', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                       dir_fd=self.ledger_fd)
        try:
            info = os.fstat(lock)
            if (info.st_uid != 0 or info.st_mode & 0o077
                    or any(n.startswith('system.posix_acl_') for n in os.listxattr(lock))):
                raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BootstrapRejected('LOCK_BUSY') from exc
            self.validate_deployment()
            kernel.verify_enforcement(fd, self.device_fd)
            check_peer()
            result = self._process_locked(request, fd, check_peer=check_peer)
            check_peer()
            kernel.verify_enforcement(fd, self.device_fd)
            self.validate_deployment()
            return result
        finally:
            # Closing this independently opened FD releases only this lock.
            os.close(lock)

    def _process_locked(self, request, fd, *, check_peer):
        import re
        from api.display_bootstrap_quota_protocol import validate_request, BINDING
        validate_request(request)
        if (request['deployment_sha'] != self.config['policy_sha']
                or request['max_bytes'] != self.config['hard_bytes']):
            raise BootstrapRejected('INVALID_INPUT')
        identity = directory_identity(fd)
        if (identity != request['directory_identity']
                or not stat.S_ISDIR(identity['mode'])
                or identity['uid'] != self.config['creator_uid']
                or identity['mode'] & 0o022
                or any(n.startswith('system.posix_acl_') for n in os.listxattr(fd))
                or identity['dev'] != os.fstat(self.device_fd).st_rdev):
            raise BootstrapRejected('IDENTITY_CHANGED')
        cid = request['candidate_id']
        matches = []
        for root in (self.source_fd, self.publish_fd):
            try:
                named = os.open(cid, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            except FileNotFoundError:
                continue
            try:
                matches.append((root, directory_identity(named)))
            finally:
                os.close(named)
        if len(matches) != 1 or matches[0][1] != identity:
            raise BootstrapRejected('IDENTITY_CHANGED')
        allocate = request['operation'] == 'allocate'
        if allocate and matches[0][0] != self.source_fd:
            raise BootstrapRejected('STATE_CONFLICT')
        records = ledger.validate_index(self.ledger_fd, self.config['first_project_id'],
                                        self.config['last_project_id'])
        binding = records.get(cid)
        if binding is None:
            if not allocate or os.listdir(fd) or kernel.read_project(fd)[0] != 0:
                raise BootstrapRejected('STATE_CONFLICT')
            def check_unused(project_id):
                actual = kernel.query(self.device_fd, project_id)
                if any(actual[k] for k in ('hard_bytes', 'hard_inodes', 'used_bytes',
                                          'used_inodes', 'soft_bytes', 'soft_inodes')):
                    raise BootstrapRejected('STATE_CONFLICT')
            check_peer()
            binding = ledger.reserve(self.ledger_fd, cid,
                first_id=self.config['first_project_id'], last_id=self.config['last_project_id'],
                policy_sha=self.config['policy_sha'], directory_identity=identity,
                hard_bytes=self.config['hard_bytes'], hard_inodes=self.config['hard_inodes'],
                device=identity['dev'], check_unused=check_unused)
        for key, expected in [('policy_sha', self.config['policy_sha']),
                              ('directory_identity', identity), ('device', identity['dev']),
                              ('hard_bytes', self.config['hard_bytes']),
                              ('hard_inodes', self.config['hard_inodes'])]:
            if binding[key] != expected:
                raise BootstrapRejected('IDENTITY_CHANGED')
        project_id = binding['project_id']
        current, inherit = kernel.read_project(fd)
        if allocate and current == 0:
            if os.listdir(fd):
                raise BootstrapRejected('STATE_CONFLICT')
            actual = kernel.query(self.device_fd, project_id)
            if actual['used_bytes'] or actual['used_inodes']:
                raise BootstrapRejected('STATE_CONFLICT')
            if actual['hard_bytes'] not in (0, binding['hard_bytes']) or actual['hard_inodes'] not in (0, binding['hard_inodes']):
                raise BootstrapRejected('STATE_CONFLICT')
            check_peer()
            kernel.set_limits(self.device_fd, project_id, binding['hard_bytes'], binding['hard_inodes'])
            check_peer()
            kernel.assign_empty(fd, project_id)
        if kernel.read_project(fd) != (project_id, True):
            raise BootstrapRejected('IDENTITY_CHANGED')
        actual = kernel.query(self.device_fd, project_id)
        if (actual['hard_bytes'] != binding['hard_bytes']
                or actual['hard_inodes'] != binding['hard_inodes']
                or actual['soft_bytes'] or actual['soft_inodes']
                or actual['used_bytes'] > binding['hard_bytes']
                or actual['used_inodes'] > binding['hard_inodes']):
            raise BootstrapRejected('RESOURCE_LIMIT')
        if directory_identity(fd) != identity:
            raise BootstrapRejected('IDENTITY_CHANGED')
        # A held inode can survive unlink/rename. Re-resolve the approved name
        # before returning authority; do not accept an orphaned descriptor.
        final_matches = []
        for root in (self.source_fd, self.publish_fd):
            try:
                named = os.open(cid, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            except FileNotFoundError:
                continue
            try:
                final_matches.append((root, directory_identity(named)))
            finally:
                os.close(named)
        if final_matches != matches:
            raise BootstrapRejected('IDENTITY_CHANGED')
        return dict({k: request[k] for k in BINDING}, version=2, candidate_id=cid,
                    directory_identity=identity, project_id=project_id,
                    hard_bytes=actual['hard_bytes'], used_bytes=actual['used_bytes'],
                    inherit=True, enforced=True)
