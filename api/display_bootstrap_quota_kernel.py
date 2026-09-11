"""Linux project quota operations for the privileged broker only.

Callers must authenticate the peer and validate device/directory ownership.
No path supplied by a client is accepted here. No quota removal operation.
"""
import ctypes
import fcntl
import os
import platform
import stat
import struct

from api.display_bootstrap_policy import BootstrapRejected


class _Quota(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        'hard_blocks', 'soft_blocks', 'used_bytes', 'hard_inodes',
        'soft_inodes', 'used_inodes', 'block_time', 'inode_time')]
    _fields_.append(('valid', ctypes.c_uint32))


def _call(command, device_fd, project_id, value):
    if platform.machine() != 'x86_64' or platform.system() != 'Linux':
        raise BootstrapRejected('UNSUPPORTED_PLATFORM')
    if type(project_id) is not int or not 0 < project_id < 2**31:
        raise BootstrapRejected('INVALID_INPUT')
    if not stat.S_ISBLK(os.fstat(device_fd).st_mode):
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
    libc = ctypes.CDLL(None, use_errno=True)
    libc.quotactl.argtypes = [ctypes.c_int, ctypes.c_char_p,
                              ctypes.c_int, ctypes.c_void_p]
    result = libc.quotactl(ctypes.c_int((command << 8) | 2),
                           f'/proc/self/fd/{device_fd}'.encode(), project_id,
                           ctypes.byref(value))
    if result != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))


def verify_enforcement(directory_fd, device_fd):
    """Require the held ext4 mount to advertise enforced project quotas.

    Other filesystems and accounting-only/no-enforcement mounts are rejected.
    The caller must separately pin the approved mount identity and namespace.
    """
    from pathlib import Path
    metadata = dict(line.split(':', 1) for line in
                    Path(f'/proc/self/fdinfo/{directory_fd}').read_text().splitlines())
    mount_id = int(metadata['mnt_id'])
    rows = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
    matches = [row for row in rows if int(row[0]) == mount_id]
    if len(matches) != 1:
        raise BootstrapRejected('UNSUPPORTED_PLATFORM')
    row = matches[0]
    divider = row.index('-')
    options = set(row[5].split(',') + row[divider + 3].split(','))
    device = os.fstat(device_fd)
    if (row[divider + 1] != 'ext4' or not {'rw', 'prjquota'} <= options
            or options & {'noquota', 'pqnoenforce', 'pquota,noenforce', 'ro'}
            or not stat.S_ISBLK(device.st_mode)
            or device.st_rdev != os.fstat(directory_fd).st_dev
            or row[2] != f'{os.major(device.st_rdev)}:{os.minor(device.st_rdev)}'):
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
    # Q_GETFMT also requires this quota type to be active in the kernel.
    quota_format = ctypes.c_uint32()
    _call(0x800004, device_fd, 1, quota_format)
    if quota_format.value not in (2, 4):
        raise BootstrapRejected('UNSUPPORTED_PLATFORM')
    return dict(mount_id=mount_id, device=device.st_rdev,
                quota_format=quota_format.value, enforced=True)


def read_project(directory_fd):
    if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
        raise BootstrapRejected('INVALID_INPUT')
    raw = bytearray(28)
    fcntl.ioctl(directory_fd, 0x801c581f, raw, True)
    flags, _, _, project_id, _ = struct.unpack('=IIIII8x', raw)
    return project_id, bool(flags & 0x200)


def query(device_fd, project_id):
    value = _Quota()
    _call(0x800007, device_fd, project_id, value)
    return dict(project_id=project_id, hard_bytes=value.hard_blocks * 1024,
                used_bytes=value.used_bytes, hard_inodes=value.hard_inodes,
                used_inodes=value.used_inodes,
                soft_bytes=value.soft_blocks * 1024,
                soft_inodes=value.soft_inodes)


def set_limits(device_fd, project_id, hard_bytes, hard_inodes):
    if (os.geteuid() != 0 or type(hard_bytes) is not int
            or not 0 < hard_bytes <= 9007199254740991 or hard_bytes % 1024
            or type(hard_inodes) is not int or not 0 < hard_inodes < 2**31):
        raise BootstrapRejected('INVALID_INPUT')
    value = _Quota()
    value.hard_blocks = hard_bytes // 1024
    value.hard_inodes = hard_inodes
    value.valid = 1 | 4
    _call(0x800008, device_fd, project_id, value)
    actual = query(device_fd, project_id)
    if (actual['hard_bytes'] != hard_bytes or actual['hard_inodes'] != hard_inodes
            or actual['soft_bytes'] or actual['soft_inodes']):
        raise BootstrapRejected('RESOURCE_LIMIT')
    return actual


def assign_empty(directory_fd, project_id):
    if os.geteuid() != 0 or type(project_id) is not int or not 0 < project_id < 2**31:
        raise BootstrapRejected('INVALID_INPUT')
    if os.listdir(directory_fd):
        raise BootstrapRejected('STATE_CONFLICT')
    raw = bytearray(28)
    fcntl.ioctl(directory_fd, 0x801c581f, raw, True)
    flags, extent, count, current, cow = struct.unpack('=IIIII8x', raw)
    if current not in (0, project_id):
        raise BootstrapRejected('IDENTITY_CHANGED')
    fcntl.ioctl(directory_fd, 0x401c5820,
                struct.pack('=IIIII8x', flags | 0x200, extent, count, project_id, cow))
    os.fsync(directory_fd)
    if read_project(directory_fd) != (project_id, True):
        raise BootstrapRejected('IDENTITY_CHANGED')
