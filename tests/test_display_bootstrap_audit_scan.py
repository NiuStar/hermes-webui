"""Whole-container fail-closed scan; no filesystem admission mocks."""
import stat

import pytest

from api.display_bootstrap_audit_codec import SLOT_BYTES, encode_header, encode_slot
from api.display_bootstrap_audit_scan import scan
from api.display_bootstrap_manifest import canonical_bytes, digest


def container(records):
    header = dict(format_version=1, candidate_id='a' * 32,
                  file_identity=dict(dev=1, ino=2, uid=1000, gid=1000,
                                     mode=stat.S_IFREG | 0o600, nlink=1),
                  deployment_sha='b' * 64, resource_sha='c' * 64,
                  slot_count=9, slot_bytes=SLOT_BYTES, payload_bytes=65536)
    raw = encode_header(header)
    sha = raw[4064:].hex()
    for index, payload in enumerate(records):
        raw += bytes(SLOT_BYTES) if payload is None else encode_slot(header, sha, index, 1, payload)
    raw += bytes(SLOT_BYTES * (9 - len(records)))
    return raw


def record(state='RESERVED', seq=1, previous_sha=None):
    return canonical_bytes(dict(format_version=1, candidate_id='a' * 32, seq=seq,
                                previous_sha=previous_sha, state=state,
                                target_name='a' * 32, manifest_sha=None,
                                approval_id=None, error_code=None))


def read(raw):
    return scan(lambda offset, length: raw[offset:offset + length])


def test_reserved_building_prefix():
    first = record()
    result = read(container([first, record('BUILDING', 2, digest(first))]))
    assert result['registry_last']['state'] == 'BUILDING'
    assert result['remaining_slots'] == 7
    assert len(result['receipts']) == 2
    assert all('payload' not in r for r in result['receipts'])


def test_zero_gap_not_skipped():
    first = record()
    with pytest.raises(ValueError):
        read(container([first, None, record('BUILDING', 2, digest(first))]))


def test_torn_tail_prevents_return_of_valid_prefix():
    raw = bytearray(container([record()]))
    raw[4096 + SLOT_BYTES] = 1
    with pytest.raises(ValueError):
        read(bytes(raw))


def test_wrong_previous_digest():
    with pytest.raises(ValueError):
        read(container([record(), record('BUILDING', 2, 'f' * 64)]))


def test_duplicate_sequence():
    with pytest.raises(ValueError):
        read(container([record(), record()]))


def test_short_read_rejected():
    with pytest.raises(ValueError):
        read(container([record()])[:-1])
