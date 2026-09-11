"""Finite READY/GO/ARMED pipe records; no business data or implicit GO."""
import os
import select
import stat
import struct
import time
from api.display_bootstrap_manifest import canonical_bytes, parse_record
from api.display_bootstrap_v3 import hex_value
from api.display_bootstrap_v4 import checked

FIELDS = {'version', 'type', 'batch_id', 'connection_nonce', 'role', 'pid', 'starttime', 'policy_sha'}
ROLES = {'creator', 'publisher', 'recover', 'approver', 'broker', 'observer', 'helper', 'test'}


def validate(value):
    if type(value) is not dict or set(value) != FIELDS:
        raise ValueError('INVALID_INPUT')
    if (type(value['version']) is not int or value['version'] != 1
            or type(value['type']) is not str or value['type'] not in ('READY', 'GO', 'ARMED')
            or type(value['role']) is not str or value['role'] not in ROLES):
        raise ValueError('INVALID_INPUT')
    for key in ('pid', 'starttime'):
        if checked(value[key]) < 1:
            raise ValueError('INVALID_INPUT')
    for key in ('batch_id', 'connection_nonce', 'policy_sha'):
        hex_value(value[key], 64 if key == 'policy_sha' else 32)
    return value


def _wait(fd, deadline, writing=False):
    if not stat.S_ISFIFO(os.fstat(fd).st_mode):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError('RESOURCE_LIMIT')
    readable, writable, _ = select.select([] if writing else [fd], [fd] if writing else [], [], remaining)
    if not (writable if writing else readable) or time.monotonic() >= deadline:
        raise ValueError('RESOURCE_LIMIT')


def send(fd, value, *, deadline):
    """Consumes its exclusive pipe write end even on validation failure."""
    try:
        raw = canonical_bytes(validate(value))
        if not 1 <= len(raw) <= 1024:
            raise ValueError('RESOURCE_LIMIT')
        packet = struct.pack('!I', len(raw)) + raw
        if len(packet) > os.fpathconf(fd, 'PC_PIPE_BUF'):
            raise ValueError('UNSUPPORTED_PLATFORM')
        _wait(fd, deadline, writing=True)
        if os.write(fd, packet) != len(packet):
            raise ValueError('IO_FAILURE')
        if time.monotonic() >= deadline:
            raise ValueError('RESOURCE_LIMIT')
    finally:
        os.close(fd)


def receive(fd, expected, *, deadline):
    """Consumes read end. Exact expected record must come from trusted launcher state."""
    try:
        validate(expected)
        def exact(size):
            result = bytearray()
            while len(result) < size:
                _wait(fd, deadline)
                raw = os.read(fd, size - len(result))
                if not raw:
                    raise ValueError('IO_FAILURE')
                result.extend(raw)
            return bytes(result)
        size, = struct.unpack('!I', exact(4))
        if not 1 <= size <= 1024:
            raise ValueError('INVALID_INPUT')
        value = validate(parse_record(exact(size)))
        if value != expected:
            raise ValueError('APPROVAL_MISMATCH')
        _wait(fd, deadline)
        if os.read(fd, 1):
            raise ValueError('INVALID_INPUT')
        if time.monotonic() >= deadline:
            raise ValueError('RESOURCE_LIMIT')
        return value
    finally:
        os.close(fd)
