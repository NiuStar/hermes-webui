"""Read internal ext4 journal metadata from a held read-only device fd.

Supports extent-mapped journal inodes, conventional group descriptors and
4096-byte blocks only. Unknown layouts are rejected rather than guessed.
"""
import os
import struct

from api.display_bootstrap_ext4 import decode_superblock
from api.display_bootstrap_audit_log import _deadline


def observe_device(fd, device_bytes, deadline):
    def read(offset, size):
        if offset < 0 or size <= 0 or offset + size > device_bytes:
            raise ValueError('UNSUPPORTED_PLATFORM')
        _deadline(deadline)
        raw = os.pread(fd, size, offset)
        _deadline(deadline)
        if len(raw) != size:
            raise ValueError('IO_FAILURE')
        return raw
    super_raw = read(1024, 1024)
    facts = decode_superblock(super_raw)
    incompat = facts['features']['incompat']
    # META_BG, compression, journal-dev, inline-data and encryption unsupported.
    if incompat & (0x10 | 0x1 | 0x8 | 0x8000 | 0x10000):
        raise ValueError('UNSUPPORTED_PLATFORM')
    if facts['blocks'] * 4096 > device_bytes:
        raise ValueError('IDENTITY_CHANGED')
    inode_number = facts['journal_inode']
    group, index = divmod(inode_number - 1, facts['inodes_per_group'])
    groups = (facts['blocks'] + facts['blocks_per_group'] - 1) // facts['blocks_per_group']
    if group >= groups:
        raise ValueError('UNSUPPORTED_PLATFORM')
    desc_size = struct.unpack_from('<H', super_raw, 0xFE)[0] if incompat & 0x80 else 32
    if desc_size not in (32, 64):
        raise ValueError('UNSUPPORTED_PLATFORM')
    desc = read(4096 + group * desc_size, desc_size)
    table = struct.unpack_from('<I', desc, 8)[0]
    if desc_size == 64:
        table |= struct.unpack_from('<I', desc, 40)[0] << 32
    if not 0 < table < facts['blocks']:
        raise ValueError('UNSUPPORTED_PLATFORM')
    inode_offset = table * 4096 + index * facts['inode_bytes']
    inode = read(inode_offset, facts['inode_bytes'])
    mode = struct.unpack_from('<H', inode, 0)[0]
    flags = struct.unpack_from('<I', inode, 32)[0]
    if mode & 0xF000 != 0x8000 or not flags & 0x80000:
        raise ValueError('UNSUPPORTED_PLATFORM')
    tree = inode[40:100]
    seen = set()
    expected_depth = None
    for _ in range(6):
        magic, entries, maximum, depth, generation = struct.unpack_from('<HHHHI', tree)
        if (magic != 0xF30A or not 1 <= entries <= maximum
                or maximum > (len(tree) - 12) // 12 or depth > 5
                or (expected_depth is not None and depth != expected_depth)):
            raise ValueError('UNSUPPORTED_PLATFORM')
        logicals = [struct.unpack_from('<I', tree, 12 + n*12)[0] for n in range(entries)]
        if logicals[0] != 0 or logicals != sorted(set(logicals)):
            raise ValueError('UNSUPPORTED_PLATFORM')
        if depth == 0:
            logical, length, high, low = struct.unpack_from('<IHHI', tree, 12)
            # ee_len > 32768 is unwritten. 32768 itself is initialized.
            if not 0 < length <= 32768:
                raise ValueError('UNSUPPORTED_PLATFORM')
            block = (high << 32) | low
            if not 0 < block < facts['blocks'] or block + length > facts['blocks']:
                raise ValueError('UNSUPPORTED_PLATFORM')
            journal = read(block * 4096, 4096)
            break
        logical, low, high, unused = struct.unpack_from('<IIHH', tree, 12)
        child = (high << 32) | low
        if unused or child in seen or not 0 < child < facts['blocks']:
            raise ValueError('UNSUPPORTED_PLATFORM')
        seen.add(child)
        expected_depth = depth - 1
        tree = read(child * 4096, 4096)
    else:
        raise ValueError('UNSUPPORTED_PLATFORM')
    magic, kind, sequence, blocksize, maxlen, first, seq, start, error = struct.unpack_from('>9I', journal)
    if (magic != 0xC03B3998 or kind != 4 or blocksize != 4096
            or not 0 < first < maxlen or start >= maxlen or error != 0):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    if journal[48:64] != super_raw[0x68:0x78]:
        raise ValueError('IDENTITY_CHANGED')
    # Mutable journal sequence/start may advance on a live mount. Bind stable
    # metadata only; errors must remain zero and inode mapping unchanged.
    after = decode_superblock(read(1024, 1024))
    if after != facts or read(inode_offset, facts['inode_bytes'])[40:100] != inode[40:100]:
        raise ValueError('IDENTITY_CHANGED')
    import uuid
    facts.update(journal_uuid=str(uuid.UUID(bytes=journal[48:64])),
                 journal_block_bytes=blocksize, journal_max_blocks=maxlen,
                 journal_features=dict(zip(('compat','incompat','readonly'),
                                           struct.unpack_from('>III', journal, 36))))
    return facts
