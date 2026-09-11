"""Length-prefixed canonical JSON for the fixed storage observation channel."""
import socket
import struct
import time

from api.display_bootstrap_manifest import canonical_bytes, parse_record
from api.display_bootstrap_storage_contract import _hex
from api.display_bootstrap_audit_log import _deadline


MAX_FRAME = 65536


def _timeout(sock, deadline):
    _deadline(deadline)
    sock.settimeout(max(0.001, deadline - time.monotonic()))


def receive(sock, deadline):
    def exact(size):
        pieces = []
        while size:
            _timeout(sock, deadline)
            raw = sock.recv(size)
            if not raw:
                raise ValueError('IO_FAILURE')
            pieces.append(raw)
            size -= len(raw)
        _deadline(deadline)
        return b''.join(pieces)
    size, = struct.unpack('!I', exact(4))
    if not 0 < size <= MAX_FRAME:
        raise ValueError('INVALID_INPUT')
    return parse_record(exact(size))


def send(sock, value, deadline):
    raw = canonical_bytes(value)
    _timeout(sock, deadline)
    sock.sendall(struct.pack('!I', len(raw)) + raw)
    _deadline(deadline)


def validate_request(value):
    if (type(value) is not dict or set(value) != {'version', 'op', 'profile_id', 'nonce'}
            or type(value['version']) is not int or value['version'] != 1
            or value['op'] != 'OBSERVE'):
        raise ValueError('INVALID_INPUT')
    _hex(value['profile_id'], 32)
    _hex(value['nonce'], 32)
    return value


def request_observation(profile_id, profile_sha, *, deadline, verify_peer):
    """Caller provides root service identity verifier, never a cached boolean."""
    import secrets
    _hex(profile_id, 32)
    _hex(profile_sha, 64)
    if not callable(verify_peer):
        raise ValueError('INVALID_INPUT')
    request = dict(version=1, op='OBSERVE', profile_id=profile_id,
                   nonce=secrets.token_hex(16))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        _timeout(sock, deadline)
        sock.connect('/run/hermes-display-bootstrap/storage.sock')
        pid, uid, gid = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != 0 or pid <= 0:
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        verify_peer(pid, uid, gid, deadline)
        send(sock, request, deadline)
        response = receive(sock, deadline)
        if (set(response) != {'version', 'nonce', 'profile_sha', 'observation'}
                or type(response['version']) is not int or response['version'] != 1
                or response['nonce'] != request['nonce'] or response['profile_sha'] != profile_sha
                or type(response['observation']) is not dict):
            raise ValueError('APPROVAL_MISMATCH')
        verify_peer(pid, uid, gid, deadline)
        # The protocol is one frame per connection, not a reusable multiplexed capability.
        _timeout(sock, deadline)
        if sock.recv(1):
            raise ValueError('INVALID_INPUT')
        return response['observation']
