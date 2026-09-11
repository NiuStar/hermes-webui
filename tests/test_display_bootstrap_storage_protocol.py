"""Bounded storage wire tests; no daemon authentication claims."""
import struct
import time
import pytest
from api.display_bootstrap_storage_protocol import receive, send, validate_request
from api.display_bootstrap_manifest import canonical_bytes


class Stream:
    def __init__(self, raw=b''):
        self.raw = raw
        self.sent = b''
    def settimeout(self, timeout):
        assert timeout > 0
    def recv(self, length):
        raw, self.raw = self.raw[:min(length, 3)], self.raw[min(length, 3):]
        return raw
    def sendall(self, raw):
        self.sent += raw


def test_fragmented_canonical_request():
    value = dict(version=1, op='OBSERVE', profile_id='a'*32, nonce='b'*32)
    raw = canonical_bytes(value)
    stream = Stream(struct.pack('!I', len(raw)) + raw)
    assert validate_request(receive(stream, time.monotonic()+10)) == value
    send(stream, value, time.monotonic()+10)
    assert stream.sent == struct.pack('!I', len(raw)) + raw


@pytest.mark.parametrize('size', [0, 65537, 0xffffffff])
def test_oversized_frames_rejected_before_payload(size):
    with pytest.raises(ValueError):
        receive(Stream(struct.pack('!I', size)), time.monotonic()+10)


def test_truncated_payload():
    with pytest.raises(ValueError):
        receive(Stream(struct.pack('!I', 100)+b'{}'), time.monotonic()+10)


@pytest.mark.parametrize('changes', [dict(version=True), dict(op='WRITE'),
                                    dict(profile_id='../escape'), dict(nonce='B'*32)])
def test_fixed_request_contract(changes):
    with pytest.raises(ValueError):
        validate_request(dict(dict(version=1,op='OBSERVE',profile_id='a'*32,nonce='b'*32), **changes))
