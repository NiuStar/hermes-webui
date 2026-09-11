"""Irreversible Linux worker restriction; install before untrusted work.

Native ABI only (libseccomp kills other ABIs). Blocks quota-mutating ioctls,
new file_setattr interface and asynchronous syscall bypass. Read-only ioctl
inspection remains available. This is not, alone, context admission.
"""
import ctypes
import errno
import platform
import os

_INSTALLED_PID = None


def verify_quota_guard():
    """Negative-FD probes are non-mutating; mode flags alone are not proof."""
    from pathlib import Path
    status = dict(line.split(':', 1) for line in
                  Path('/proc/self/status').read_text().splitlines() if ':' in line)
    if status.get('NoNewPrivs', '').strip() != '1' or status.get('Seccomp', '').strip() != '2':
        raise RuntimeError('ACCESS_BOUNDARY_UNPROVEN')
    libc = ctypes.CDLL(None, use_errno=True)
    for request in (0x401c5820, 0x1401c5820, 0x40086602, 0x40046602):
        ctypes.set_errno(0)
        result = libc.syscall(ctypes.c_long(16), ctypes.c_int(-1),
                              ctypes.c_ulong(request), ctypes.c_void_p())
        if result != -1 or ctypes.get_errno() != errno.EPERM:
            raise RuntimeError('ACCESS_BOUNDARY_UNPROVEN')
    return {'quota_guard': 'seccomp-v1', 'no_new_privileges': True}


def install_quota_guard():
    global _INSTALLED_PID
    if _INSTALLED_PID == os.getpid():
        # Avoid exhausting the kernel filter budget on every revalidation.
        # The marker never substitutes for live kernel probes.
        verify_quota_guard()
        return
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise RuntimeError('UNSUPPORTED_PLATFORM')
    lib = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_attr_set.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    class Comparison(ctypes.Structure):
        _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_int),
                    ('a', ctypes.c_uint64), ('b', ctypes.c_uint64)]
    lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                         ctypes.c_int, ctypes.c_uint, ctypes.POINTER(Comparison)]
    ctx = lib.seccomp_init(0x7fff0000)
    if not ctx:
        raise RuntimeError('ACCESS_BOUNDARY_UNPROVEN')
    def checked(result):
        if result != 0:
            raise RuntimeError('ACCESS_BOUNDARY_UNPROVEN')
    try:
        # Synchronize all threads; fail rather than leave an unrestricted thread.
        checked(lib.seccomp_attr_set(ctx, 4, 1))
        deny = 0x00050000 | errno.EPERM
        ioctl = lib.seccomp_syscall_resolve_name(b'ioctl')
        for request in (0x401c5820, 0x40086602, 0x40046602):
            # Kernel ioctl command is 32-bit: mask upper bits to avoid bypass.
            comparison = Comparison(1, 7, 0xffffffff, request)
            checked(lib.seccomp_rule_add_array(ctx, deny, ioctl, 1, ctypes.byref(comparison)))
        for name in ('quotactl', 'quotactl_fd', 'io_uring_setup', 'io_uring_enter',
                     'io_uring_register', 'ptrace', 'process_vm_writev'):
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number < 0:
                raise RuntimeError('UNSUPPORTED_PLATFORM')
            checked(lib.seccomp_rule_add_array(ctx, deny, number, 0, None))
        # x86_64 Linux file_setattr: future API, blocked even on older kernels.
        checked(lib.seccomp_rule_add_array(ctx, deny, 545, 0, None))
        checked(lib.seccomp_load(ctx))  # libseccomp also establishes no_new_privs
        verify_quota_guard()
        _INSTALLED_PID = os.getpid()
    finally:
        lib.seccomp_release(ctx)
