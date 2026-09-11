"""Trusted same-process credential transition before any business input."""
import ctypes
import os
from pathlib import Path


def drop_identity(uid, gid, supplementary_gids):
    if (os.geteuid() != 0 or type(uid) is not int or type(gid) is not int
            or uid <= 0 or gid <= 0 or type(supplementary_gids) is not list
            or any(type(g) is not int or g <= 0 for g in supplementary_gids)
            or supplementary_gids != sorted(set(supplementary_gids))):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    if len(list(Path('/proc/self/task').iterdir())) != 1:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    # Clear ambient set and all supported capability bounding bits before UID drop.
    if libc.prctl(47, 4, 0, 0, 0) != 0:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    last = int(Path('/proc/sys/kernel/cap_last_cap').read_text().strip())
    if not 0 <= last <= 63:
        raise ValueError('UNSUPPORTED_PLATFORM')
    for cap in range(last+1):
        if libc.prctl(24, cap, 0, 0, 0) != 0:
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    os.setgroups(supplementary_gids)
    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)
    # capset explicitly clears inherited capabilities as well as permitted/effective.
    class Header(ctypes.Structure):
        _fields_ = [('version', ctypes.c_uint32), ('pid', ctypes.c_int)]
    class Data(ctypes.Structure):
        _fields_ = [('effective', ctypes.c_uint32), ('permitted', ctypes.c_uint32), ('inheritable', ctypes.c_uint32)]
    header, data = Header(0x20080522, 0), (Data * 2)()
    if libc.capset(ctypes.byref(header), ctypes.byref(data)) != 0:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    verify_identity(uid, gid, supplementary_gids)


def verify_identity(uid, gid, supplementary_gids):
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
    if (tuple(map(int, status['Uid'].split())) != (uid,)*4
            or tuple(map(int, status['Gid'].split())) != (gid,)*4
            or sorted(map(int, status['Groups'].split())) != supplementary_gids
            or any(int(status[key].strip(), 16) for key in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'))
            or status['NoNewPrivs'].strip() != '1'
            or len(list(Path('/proc/self/task').iterdir())) != 1):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
