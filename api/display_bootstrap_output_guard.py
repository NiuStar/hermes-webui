"""Trusted startup's irreversible application logging/connection boundary.

Install only after fixed RPC endpoints and all required helpers are ready.
This filter supplements mount/FD isolation; it does not certify that isolation.
"""
import ctypes
import errno
import os
import platform
import socket


class _Comparison(ctypes.Structure):
    _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_int),
                ('a', ctypes.c_uint64), ('b', ctypes.c_uint64)]


def install_output_guard(role, *, listener_fd=None):
    roles = ('creator', 'publisher', 'recover', 'approver', 'broker', 'observer', 'helper', 'test')
    if role not in roles:
        raise ValueError('INVALID_INPUT')
    listening = role in ('broker', 'observer')
    if listening:
        if type(listener_fd) is not int or listener_fd < 3:
            raise ValueError('INVALID_INPUT')
        with socket.socket(fileno=os.dup(listener_fd)) as probe:
            if (probe.family != socket.AF_UNIX or probe.type not in (socket.SOCK_STREAM, socket.SOCK_SEQPACKET)
                    or probe.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1):
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    elif listener_fd is not None:
        raise ValueError('INVALID_INPUT')
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise ValueError('UNSUPPORTED_PLATFORM')
    lib = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_attr_set.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
        ctypes.c_int, ctypes.c_uint, ctypes.POINTER(_Comparison)]
    ctx = lib.seccomp_init(0x7fff0000)
    if not ctx:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')

    def check(result):
        if result != 0:
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')

    def deny(name, *comparisons):
        number = lib.seccomp_syscall_resolve_name(name.encode('ascii'))
        if number < 0:
            raise ValueError('UNSUPPORTED_PLATFORM')
        args = (_Comparison * len(comparisons))(*comparisons) if comparisons else None
        check(lib.seccomp_rule_add_array(ctx, 0x00050000 | errno.EPERM,
                                        number, len(comparisons), args))
    try:
        check(lib.seccomp_attr_set(ctx, 4, 1))  # TSYNC, no unrestricted sibling threads.
        for name in ('socket', 'socketpair', 'connect', 'bind', 'listen', 'accept',
                     'execve', 'execveat', 'setns', 'unshare', 'mount', 'umount2',
                     'sendto', 'sendmmsg', 'io_uring_setup', 'io_uring_enter',
                     'io_uring_register', 'ptrace', 'process_vm_writev', 'pidfd_getfd',
                     'fork', 'vfork', 'clone', 'clone3', 'prctl', 'capset',
                     'pivot_root', 'fsopen', 'fsconfig', 'fsmount', 'fspick',
                     'open_tree', 'move_mount', 'mount_setattr',
                     'setuid', 'setgid', 'setreuid', 'setregid', 'setresuid', 'setresgid',
                     'setfsuid', 'setfsgid', 'setgroups'):
            deny(name)
        if listening:
            deny('accept4', _Comparison(0, 1, listener_fd, 0))  # NE listener
            deny('accept4', _Comparison(3, 7, socket.SOCK_CLOEXEC, 0))
            for name in ('dup2', 'dup3'):
                deny(name, _Comparison(1, 4, listener_fd, 0))  # EQ destination
        else:
            deny('accept4')
        check(lib.seccomp_load(ctx))
    finally:
        lib.seccomp_release(ctx)


def clear_logging_environment():
    for key in ('NOTIFY_SOCKET', 'JOURNAL_STREAM', 'SYSLOG_IDENTIFIER'):
        os.environ.pop(key, None)
