"""Pure codec cases; no context issuance or kernel admission claims."""
import hashlib
import stat
import struct

import pytest

from api.display_bootstrap_audit_codec import (
    SLOT_BYTES, decode_header, decode_slot, encode_header, encode_slot, file_bytes,
)
from api.display_bootstrap_manifest import canonical_bytes


@pytest.fixture
def header():
    return dict(format_version=1, candidate_id='a' * 32,
                file_identity=dict(dev=1, ino=2, uid=1000, gid=1000,
                                   mode=stat.S_IFREG | 0o600, nlink=1),
                deployment_sha='b' * 64, resource_sha='c' * 64,
                slot_count=9, slot_bytes=73728, payload_bytes=65536)


def reserved(header):
    return canonical_bytes(dict(format_version=1, candidate_id=header['candidate_id'],
                                seq=1, previous_sha=None, state='RESERVED',
                                target_name=header['candidate_id'], manifest_sha=None,
                                approval_id=None, error_code=None))


def test_header_independent_layout(header):
    raw = encode_header(header)
    assert len(raw) == 4096
    assert raw[:8] == b'HBAUD001'
    size, = struct.unpack('<I', raw[8:12])
    assert raw[12:12 + size] == canonical_bytes(header)
    assert not any(raw[12 + size:4064])
    assert raw[4064:] == hashlib.sha256(raw[:4064]).digest()
    assert decode_header(raw) == header
    assert file_bytes(9) == 667648


@pytest.mark.parametrize('slots', [True, False, 0, 8, 4097, 9.0, '9', None])
def test_bad_slot_counts(slots):
    with pytest.raises(ValueError):
        file_bytes(slots)


def test_slot_independent_layout(header):
    sha = encode_header(header)[4064:].hex()
    payload = reserved(header)
    raw = encode_slot(header, sha, 0, 1, payload)
    fields = struct.unpack('<8sIIII16s32s32s32s', raw[:136])
    assert fields[:5] == (b'HBCLM001', 1, 0, 1, len(payload))
    assert fields[5] == bytes.fromhex('a' * 32)
    assert fields[6] == hashlib.sha256(b'registry:00000000000000000001').digest()
    assert fields[7] == hashlib.sha256(payload).digest()
    assert fields[8] == bytes.fromhex(sha)
    assert raw[4096:4096 + len(payload)] == payload
    assert raw[-4096:-4088] == b'HBCMT001'
    assert decode_slot(header, sha, 0, raw)['payload'] == payload
    assert decode_slot(header, sha, 1, bytes(SLOT_BYTES)) is None


@pytest.mark.parametrize('offset', [0, 8, 12, 16, 20, 24, 40, 72, 104, 136,
                                    4064, 4096, 69000, 69632, 73727])
def test_corruption_rejected(header, offset):
    sha = encode_header(header)[4064:].hex()
    raw = bytearray(encode_slot(header, sha, 0, 1, reserved(header)))
    raw[offset] ^= 1
    with pytest.raises(ValueError):
        decode_slot(header, sha, 0, bytes(raw))


def test_claim_without_commit_is_not_unused(header):
    sha = encode_header(header)[4064:].hex()
    raw = encode_slot(header, sha, 0, 1, reserved(header))
    with pytest.raises(ValueError):
        decode_slot(header, sha, 0, raw[:-4096] + bytes(4096))


def test_cross_container_and_wrong_index(header):
    sha = encode_header(header)[4064:].hex()
    raw = encode_slot(header, sha, 0, 1, reserved(header))
    with pytest.raises(ValueError):
        decode_slot(header, sha, 1, raw)
    other = dict(header, candidate_id='d' * 32)
    with pytest.raises(ValueError):
        decode_slot(other, encode_header(other)[4064:].hex(), 0, raw)


def test_nonzero_header_padding_rejected_even_with_valid_sha(header):
    raw = bytearray(encode_header(header))
    raw[4063] = 1
    raw[4064:] = hashlib.sha256(raw[:4064]).digest()
    with pytest.raises(ValueError):
        decode_header(bytes(raw))
