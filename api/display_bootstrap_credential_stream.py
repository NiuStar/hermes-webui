"""Bounded credential-bearing stream frames. No service identity authority."""
import array
import socket
import struct
import time
from api.display_bootstrap_manifest import canonical_bytes, parse_record
from api.display_bootstrap_audit_log import _deadline

MAX_FRAME = 65536


class CredentialStream:
    def __init__(self, sock, deadline, *, strict=False):
        self.sock = sock
        self.deadline = deadline
        self.strict = strict
        self.peer = None
        if strict:
            # Launcher enables PASSCRED before connect/accept, not after GO.
            if sock.getsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED) != 1:
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
            self.peer = struct.unpack('3i', sock.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if self.peer[0] <= 0:
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)

    def timeout(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('RESOURCE_LIMIT')
        self.sock.settimeout(remaining)

    def chunk(self, size):
        self.timeout()
        raw, ancillary, flags, _ = self.sock.recvmsg(
            1 if self.strict else size, socket.CMSG_SPACE(12) + socket.CMSG_SPACE(253 * array.array('i').itemsize),
            socket.MSG_CMSG_CLOEXEC)
        credentials = []
        invalid = bool(flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC))
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                received = array.array('i')
                received.frombytes(data[:len(data) - len(data) % received.itemsize])
                import os
                for fd in received:
                    os.close(fd)
                invalid = True
            elif level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(data) == 12:
                credentials.append(struct.unpack('3i', data))
            else:
                invalid = True
        _deadline(self.deadline)
        if not raw:
            # Linux may attach one all-zero credential record to stream EOF.
            # EOF is cleanup only; it never updates or authenticates the peer.
            if invalid or credentials not in ([], [(0, 0, 0)]):
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
            return b''
        if invalid or len(credentials) != 1 or credentials[0][0] <= 0:
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        if self.peer is None:
            self.peer = credentials[0]
        elif self.peer != credentials[0]:
            raise ValueError('IDENTITY_CHANGED')
        return raw

    def exact(self, size):
        data = bytearray()
        while len(data) < size:
            piece = self.chunk(size - len(data))
            if not piece:
                raise ValueError('IO_FAILURE')
            data.extend(piece)
        return bytes(data)

    def receive(self):
        size, = struct.unpack('!I', self.exact(4))
        if not 1 <= size <= MAX_FRAME:
            raise ValueError('INVALID_INPUT')
        return parse_record(self.exact(size))

    def send(self, value):
        raw = canonical_bytes(value)
        if not 1 <= len(raw) <= MAX_FRAME:
            raise ValueError('INVALID_INPUT')
        pending = memoryview(struct.pack('!I', len(raw)) + raw)
        while pending:
            self.timeout()
            sent = self.sock.sendmsg([pending])
            if sent <= 0:
                raise ValueError('IO_FAILURE')
            pending = pending[sent:]
            _deadline(self.deadline)

    def eof(self):
        if self.chunk(1):
            raise ValueError('INVALID_INPUT')
