"""Bounded Linux x86_64 FIEMAP validation, not storage qualification.

Reject unknown allocation properties. A valid mapping does not prove backing
hardware flush behavior or protect against trusted administrator changes.
"""
import fcntl
import os
import platform
import stat
import struct

from api.display_bootstrap_audit_log import _deadline

_FIEMAP = 0xC020660B
_HEADER = struct.Struct('<QQIIII')
_EXTENT = struct.Struct('<QQQQQIIII')
_BATCH = 256
_MAX = 65536
_SYNC = 1
_LAST = 1


def _mapping(fd, size, deadline):
    start = 0
    result = []
    while start < size:
        _deadline(deadline)
        request = bytearray(_HEADER.size + _BATCH * _EXTENT.size)
        _HEADER.pack_into(request, 0, start, size - start, _SYNC, 0, _BATCH, 0)
        fcntl.ioctl(fd, _FIEMAP, request, True)
        _deadline(deadline)
        requested_start, length, flags, count, capacity, reserved = _HEADER.unpack_from(request)
        if (requested_start != start or length != size - start or flags != _SYNC
                or capacity != _BATCH or reserved != 0 or not 1 <= count <= _BATCH
                or len(result) + count > _MAX):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        terminal = False
        for index in range(count):
            logical, physical, extent_len, r1, r2, bits, r3, r4, r5 = _EXTENT.unpack_from(
                request, _HEADER.size + index * _EXTENT.size)
            if (r1 or r2 or r3 or r4 or r5 or bits & ~_LAST
                    or logical != start or physical == 0 or extent_len == 0
                    or any(v % 4096 for v in (logical, physical, extent_len))
                    or logical + extent_len > size
                    or physical + extent_len >= 1 << 64):
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
            terminal = bool(bits & _LAST)
            if terminal and index != count - 1:
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
            result.append((logical, physical, extent_len, bits))
            start = logical + extent_len
        if terminal:
            if start != size:
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
            return tuple(result)
    # Full coverage without LAST is not a complete mapping proof.
    raise ValueError('ACCESS_BOUNDARY_UNPROVEN')


def verify_allocated(fd, size, deadline):
    """Require two identical written, unshared, aligned full-file mappings."""
    _deadline(deadline)
    if (platform.system() != 'Linux' or platform.machine() != 'x86_64'
            or type(size) is not int or size <= 0 or size % 4096):
        raise ValueError('UNSUPPORTED_PLATFORM')
    before = os.fstat(fd)
    if (not stat.S_ISREG(before.st_mode) or before.st_size != size
            or before.st_nlink != 1 or before.st_blocks * 512 < size):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    first = _mapping(fd, size, deadline)
    second = _mapping(fd, size, deadline)
    after = os.fstat(fd)
    fields = ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_nlink',
              'st_size', 'st_blocks', 'st_mtime_ns', 'st_ctime_ns')
    if first != second or any(getattr(before, k) != getattr(after, k) for k in fields):
        raise ValueError('IDENTITY_CHANGED')
    _deadline(deadline)
    return first
