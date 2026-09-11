"""Independent raw ext4 vectors. These are not observed deployment data."""
import struct
import uuid
import pytest
from api.display_bootstrap_ext4 import decode_superblock


def superblock():
    raw = bytearray(1024)
    for offset, value in [(0x04, 1024), (0x18, 2), (0x20, 32768),
                          (0x28, 8192), (0x5C, 4), (0xE0, 8)]:
        struct.pack_into('<I', raw, offset, value)
    struct.pack_into('<H', raw, 0x38, 0xEF53)
    struct.pack_into('<H', raw, 0x58, 256)
    raw[0x68:0x78] = uuid.UUID('11111111-2222-3333-4444-555555555555').bytes
    return raw


def test_internal_journal_fields():
    result = decode_superblock(bytes(superblock()))
    assert result['journal_inode'] == 8
    assert result['block_bytes'] == 4096
    assert result['errors'] == 0


@pytest.mark.parametrize('offset,value', [(0x5C, 0), (0xE4, 1), (0xE0, 0),
                                         (0x194, 1), (0x18, 0), (0x60, 8)])
def test_invalid_or_external_journal(offset, value):
    raw = superblock()
    struct.pack_into('<I', raw, offset, value)
    with pytest.raises(ValueError):
        decode_superblock(bytes(raw))


def test_error_state():
    raw = superblock()
    struct.pack_into('<H', raw, 0x3A, 2)
    with pytest.raises(ValueError):
        decode_superblock(bytes(raw))


def test_short_superblock():
    with pytest.raises(ValueError):
        decode_superblock(bytes(1023))
