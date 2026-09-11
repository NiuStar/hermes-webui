"""Strict ext4 superblock decoder for the read-only storage observer.

This validates filesystem metadata only, not journal replay correctness,
underlying device flush semantics or the deployment's feature allowlist.
"""
import struct
import uuid


def decode_superblock(raw):
    if type(raw) is not bytes or len(raw) != 1024:
        raise ValueError('UNSUPPORTED_PLATFORM')
    def u16(offset):
        return struct.unpack_from('<H', raw, offset)[0]
    def u32(offset):
        return struct.unpack_from('<I', raw, offset)[0]
    if u16(0x38) != 0xEF53 or u32(0x18) != 2:
        raise ValueError('UNSUPPORTED_PLATFORM')
    compat, incompat, readonly = u32(0x5C), u32(0x60), u32(0x64)
    journal_inode, journal_device = u32(0xE0), u32(0xE4)
    state = u16(0x3A)
    # HAS_JOURNAL required; JOURNAL_DEV denotes an external journal device.
    if (not compat & 0x4 or incompat & 0x8 or journal_device != 0
            or journal_inode == 0 or raw[0xD0:0xE0] != bytes(16)
            or state & ~0x7 or state & 0x6 or u32(0x194) != 0):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    blocks = u32(0x04)
    if incompat & 0x80:  # 64BIT
        blocks |= u32(0x150) << 32
    if blocks == 0 or u32(0x20) == 0 or u32(0x28) == 0:
        raise ValueError('UNSUPPORTED_PLATFORM')
    inode_size = u16(0x58)
    if inode_size < 128 or inode_size > 4096 or inode_size & (inode_size - 1):
        raise ValueError('UNSUPPORTED_PLATFORM')
    fs_uuid = str(uuid.UUID(bytes=raw[0x68:0x78]))
    if fs_uuid == str(uuid.UUID(int=0)):
        raise ValueError('UNSUPPORTED_PLATFORM')
    return dict(fs_uuid=fs_uuid, block_bytes=4096, blocks=blocks,
                internal_journal=True, journal_inode=journal_inode,
                errors=u32(0x194), state=state,
                needs_recovery=bool(incompat & 0x4),
                features=dict(compat=compat, incompat=incompat, readonly=readonly),
                inode_bytes=inode_size, blocks_per_group=u32(0x20),
                inodes_per_group=u32(0x28))
