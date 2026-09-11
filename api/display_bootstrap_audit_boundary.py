"""Audit-only termination boundary: no candidate or publication capability."""
import ctypes
import os
import platform
import stat
from pathlib import Path
from api.display_bootstrap_policy import BootstrapRejected
from api.display_bootstrap_write_guard import (
    _check_descriptors, _Ruleset, _PathRule, _HANDLED, _WRITE_FILE)


def install_audit_termination_boundary(registry_fd):
    ruleset = audit_fd = None
    try:
        if (platform.system() != 'Linux' or platform.machine() != 'x86_64'
                or os.geteuid() == 0 or len(list(Path('/proc/self/task').iterdir())) != 1):
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        parent = os.fstat(registry_fd)
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
                or stat.S_IMODE(parent.st_mode) != 0o700 or os.listxattr(registry_fd)):
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        audit_fd = os.open('audit.bin', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                           dir_fd=registry_fd)
        before = os.fstat(audit_fd)
        fields = ('st_dev','st_ino','st_uid','st_gid','st_mode','st_nlink','st_size')
        identity = tuple(getattr(before, key) for key in fields)
        def unchanged():
            named = os.stat('audit.bin', dir_fd=registry_fd, follow_symlinks=False)
            if any(tuple(getattr(item,key) for key in fields) != identity
                   for item in (named, os.fstat(audit_fd))):
                raise BootstrapRejected('IDENTITY_CHANGED')
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.geteuid() or before.st_dev != parent.st_dev
                or stat.S_IMODE(before.st_mode) != 0o600 or os.listxattr(audit_fd)):
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        unchanged()
        from api.display_bootstrap_seccomp import verify_quota_guard
        verify_quota_guard()
        _check_descriptors()
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        def call(number, *args):
            result = libc.syscall(ctypes.c_long(number), *args)
            if result < 0:
                raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
            return result
        abi = call(444, ctypes.c_void_p(), ctypes.c_size_t(0), ctypes.c_uint(1))
        if abi < 3:
            raise BootstrapRejected('UNSUPPORTED_PLATFORM')
        attrs = _Ruleset(_HANDLED)
        ruleset = call(444, ctypes.byref(attrs), ctypes.c_size_t(ctypes.sizeof(attrs)), ctypes.c_uint(0))
        rule = _PathRule(_WRITE_FILE, audit_fd)
        call(445, ctypes.c_int(ruleset), ctypes.c_int(1), ctypes.byref(rule), ctypes.c_uint(0))
        call(446, ctypes.c_int(ruleset), ctypes.c_uint(0))
        unchanged()
        return dict(mode='AUDIT_TERMINATE', landlock_abi=abi, handled_access_fs=_HANDLED,
                    audit_identity={key[3:]:getattr(before,key) for key in fields},
                    allowed_access_fs=_WRITE_FILE)
    except OSError as exc:
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN') from exc
    finally:
        for fd in (ruleset, audit_fd):
            if fd is not None:
                os.close(fd)
