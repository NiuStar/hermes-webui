"""Kernel credentials via local sockets; not systemd identity acceptance."""
import array
import os
import socket
import struct
import time
import pytest
from api.display_bootstrap_credential_stream import CredentialStream


def test_real_credentials_roundtrip():
    left, right = socket.socketpair()
    with left, right:
        stream = CredentialStream(right, time.monotonic() + 2)
        left.sendall(struct.pack('!I', 7) + b'{"a":1}')
        assert stream.receive() == {'a': 1}
        assert stream.peer == (os.getpid(), os.getuid(), os.getgid())
        left.shutdown(socket.SHUT_WR)
        stream.eof()


def test_fd_injection_rejected_and_closed():
    left, right = socket.socketpair()
    with left, right:
        stream = CredentialStream(right, time.monotonic() + 2)
        fd = os.open('/dev/null', os.O_RDONLY)
        try:
            before = set(os.listdir('/proc/self/fd'))
            left.sendmsg([b'x'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [fd]))])
            with pytest.raises(ValueError, match='ACCESS_BOUNDARY_UNPROVEN'):
                stream.chunk(1)
            assert set(os.listdir('/proc/self/fd')) == before
        finally:
            os.close(fd)


def test_excessive_frame_rejected_before_body():
    left, right = socket.socketpair()
    with left, right:
        stream = CredentialStream(right, time.monotonic() + 2)
        left.sendall(struct.pack('!I', 65537))
        with pytest.raises(ValueError, match='INVALID_INPUT'):
            stream.receive()
