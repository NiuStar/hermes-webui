"""Fixed, bounded quota packets on a launcher-authenticated existing socket."""
import array
import os
import socket
import time
from api.display_bootstrap_growth_fds import receive_fds, verify_business_fds
from api.display_bootstrap_manifest import canonical_bytes, parse_record
from api.display_bootstrap_v3 import hex_value


class QuotaChannel:
    """No connect/retry. One operation, at most eight request/response pairs.

    Application handlers validate payload schemas and kernel limits separately.
    Envelope identity comes from the trusted launch control record on both ends.
    """
    def __init__(self, connection, binding, *, deadline, verify_peer):
        if connection.type != socket.SOCK_SEQPACKET or connection.family != socket.AF_UNIX:
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        expected = {'batch_id', 'connection_nonce', 'candidate_id', 'request_sha'}
        if type(binding) is not dict or set(binding) != expected:
            raise ValueError('INVALID_INPUT')
        for key, value in binding.items():
            hex_value(value, 64 if key == 'request_sha' else 32)
        self.connection, self.binding = connection, dict(binding)
        self.deadline, self.verify_peer = deadline, verify_peer
        self.pid, self.seq, self.closed = os.getpid(), 0, False
        self.check()

    def check(self):
        if self.closed or os.getpid() != self.pid:
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        self.verify_peer()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('RESOURCE_LIMIT')
        self.connection.settimeout(remaining)

    def _packet(self, payload, seq):
        if type(payload) is not dict or type(seq) is not int or not 1 <= seq <= 8:
            raise ValueError('INVALID_INPUT')
        value = dict(version=3, batch_id=self.binding['batch_id'],
                     connection_nonce=self.binding['connection_nonce'],
                     frame_seq=seq, request=payload)
        raw = canonical_bytes(value)
        if len(raw) > 16384:
            raise ValueError('RESOURCE_LIMIT')
        return raw

    def _decode(self, raw, seq):
        value = parse_record(raw)
        if (set(value) != {'version', 'batch_id', 'connection_nonce', 'frame_seq', 'request'}
                or type(value['version']) is not int or value['version'] != 3
                or type(value['frame_seq']) is not int or value['frame_seq'] != seq
                or type(value['request']) is not dict
                or any(value[k] != self.binding[k] for k in ('batch_id', 'connection_nonce'))
                or value['request'].get('candidate_id') != self.binding['candidate_id']):
            raise ValueError('APPROVAL_MISMATCH')
        return value['request']

    def request(self, payload, fds, *, validate_response):
        try:
            self.check()
            if self.seq >= 8 or len(fds) != 3:
                raise ValueError('RESOURCE_LIMIT')
            raw = self._packet(payload, self.seq + 1)
            if self.connection.sendmsg([raw], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                                array.array('i', fds))]) != len(raw):
                raise ValueError('IO_FAILURE')
            self.check()
            raw, _ = receive_fds(self.connection, max_bytes=16384, count=0)
            result = self._decode(raw, self.seq + 1)
            validate_response(result)
            self.check()
            self.seq += 1
            return result
        except BaseException:
            self.close()
            raise

    def serve_one(self, *, directory_identity, lock_identities, process):
        fds = ()
        try:
            self.check()
            if self.seq >= 8:
                raise ValueError('RESOURCE_LIMIT')
            raw, fds = receive_fds(self.connection, max_bytes=16384, count=3)
            request = self._decode(raw, self.seq + 1)
            self.check()
            verify_business_fds(fds, directory_identity=directory_identity,
                                lock_identities=lock_identities)
            result = process(request, fds)
            self.check()
            raw = self._packet(result, self.seq + 1)
            if self.connection.sendmsg([raw]) != len(raw):
                raise ValueError('IO_FAILURE')
            self.check()
            self.seq += 1
            return result
        except BaseException:
            self.close()
            raise
        finally:
            # Shared authority/deployment OFDs live through response completion.
            # Closing is mandatory; LOCK_UN would unlock the worker's copies.
            for fd in fds:
                os.close(fd)

    def close(self):
        if not self.closed:
            self.closed = True
            self.connection.close()
