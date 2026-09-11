"""Irreversible, single-process Landlock write boundary for offline creation.

ABI >=3 is required for truncate/refer mediation. Read/execute remain governed
by DAC and the runner. Only the current candidate and its audit subtree are
writable. This is not publication authority and does not issue a context.
A creator must exit before a fresh publisher can obtain different permissions.
"""
import ctypes
import errno
import os
import platform
import stat
from pathlib import Path

from api.display_bootstrap_policy import BootstrapRejected, directory_identity

# Linux UAPI landlock: filesystem access bits 0..14, ABI 1 through 3.
_WRITE_FILE = 1 << 1
_REMOVE_DIR = 1 << 4
_REMOVE_FILE = 1 << 5
_MAKE_CHAR = 1 << 6
_MAKE_DIR = 1 << 7
_MAKE_REG = 1 << 8
_MAKE_SOCK = 1 << 9
_MAKE_FIFO = 1 << 10
_MAKE_BLOCK = 1 << 11
_MAKE_SYM = 1 << 12
_REFER = 1 << 13
_TRUNCATE = 1 << 14
_HANDLED = (_WRITE_FILE | _REMOVE_DIR | _REMOVE_FILE | _MAKE_CHAR | _MAKE_DIR |
            _MAKE_REG | _MAKE_SOCK | _MAKE_FIFO | _MAKE_BLOCK | _MAKE_SYM |
            _REFER | _TRUNCATE)
# No symlinks, devices, FIFOs, sockets or new subdirectories after preparation.
_ALLOWED = _WRITE_FILE | _REMOVE_FILE | _MAKE_REG | _REFER | _TRUNCATE


class _Ruleset(ctypes.Structure):
    _fields_ = [('handled_access_fs', ctypes.c_uint64)]


class _PathRule(ctypes.Structure):
    _pack_ = 1
    _fields_ = [('allowed_access', ctypes.c_uint64), ('parent_fd', ctypes.c_int32)]


def _check_descriptors():
    """Landlock does not revoke writable FDs opened before restriction."""
    for entry in Path('/proc/self/fd').iterdir():
        try:
            fd = int(entry.name)
            info = os.fstat(fd)
            metadata = dict(line.split(':', 1) for line in
                            Path('/proc/self/fdinfo', entry.name).read_text().splitlines())
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.EBADF):
                continue  # proc directory iterator's already-closed own FD
            raise
        flags = int(metadata['flags'].strip(), 8)
        if stat.S_ISREG(info.st_mode) and flags & os.O_ACCMODE != os.O_RDONLY:
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        if (not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode)
                and fd not in (0, 1, 2)):
            # No preexisting socket/pipe/device capability beyond runner stdio.
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
    # Previously opened shared writable mappings bypass later path checks too.
    for line in Path('/proc/self/maps').read_text().splitlines():
        parts = line.split(None, 5)
        if len(parts) >= 5 and parts[1][1:2] == 'w' and parts[1][3:4] == 's':
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')


def install_creation_boundary(candidate_fd, registry_fd):
    return _install_boundary(candidate_fd, registry_fd)


def install_publication_boundary(source_fd, publish_fd, registry_fd):
    """Allow directory relocation, not writes/truncation of frozen DB bytes.

    Landlock grants relocation rights to both root subtrees, not one filename.
    The lifecycle's fixed lock, candidate identities and no-replace rename
    remain mandatory. This is not protection against a malicious publisher
    renaming other directories; no such stronger claim is made here.
    """
    return _install_boundary(source_fd, registry_fd, publish_fd=publish_fd)


def install_fixed_boundary(candidate_fd, registry_fd, *, publish_fd=None):
    """V2: grant only audit.bin WRITE_FILE, never audit directory mutation."""
    audit_fd = os.open('audit.bin', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                       | os.O_NONBLOCK, dir_fd=registry_fd)
    try:
        return _install_boundary(candidate_fd, registry_fd, publish_fd=publish_fd,
                                 audit_fd=audit_fd)
    finally:
        os.close(audit_fd)


def _install_boundary(candidate_fd, registry_fd, *, publish_fd=None, audit_fd=None):
    """Restrict this thread and descendants; reject preexisting sibling threads.

    Both subtrees and registry/evidence/.audit-reserve must already exist.
    Caller retains the fixed global lock and must use a new process to publish.
    Failure can leave a partially restricted process: it must exit, not retry
    with wider permissions. No environment/cached marker grants admission.
    """
    ruleset = None
    try:
        if (platform.system() != 'Linux' or platform.machine() != 'x86_64'
                or os.geteuid() == 0 or len(list(Path('/proc/self/task').iterdir())) != 1):
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        identities = [directory_identity(fd) for fd in (candidate_fd, registry_fd)]
        if identities[0]['dev'] == identities[1]['dev']:
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        publication_identity = None
        if publish_fd is not None:
            publication_identity = directory_identity(publish_fd)
            if (publication_identity['dev'] != identities[0]['dev']
                    or (publication_identity['dev'], publication_identity['ino']) ==
                       (identities[0]['dev'], identities[0]['ino'])):
                raise BootstrapRejected('IDENTITY_CHANGED')
        checked_fds = [(candidate_fd, identities[0]), (registry_fd, identities[1])]
        if publish_fd is not None:
            checked_fds.append((publish_fd, publication_identity))
        for fd, identity in checked_fds:
            protected_mode = (stat.S_IMODE(identity['mode']) == 0o700
                              if fd == registry_fd or publish_fd is None
                              else not identity['mode'] & 0o022)
            if (not stat.S_ISDIR(identity['mode']) or identity['uid'] != os.geteuid()
                    or not protected_mode
                    or any(n.startswith('system.posix_acl_') for n in os.listxattr(fd))):
                raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        for name in (() if audit_fd is not None else ('evidence', '.audit-reserve')):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=registry_fd)
            try:
                if os.fstat(child).st_dev != identities[1]['dev']:
                    raise BootstrapRejected('IDENTITY_CHANGED')
            finally:
                os.close(child)
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
        attributes = _Ruleset(_HANDLED)
        ruleset = call(444, ctypes.byref(attributes), ctypes.c_size_t(ctypes.sizeof(attributes)), ctypes.c_uint(0))
        permissions = [(candidate_fd, _ALLOWED), (registry_fd, _ALLOWED)]
        if publish_fd is not None:
            relocate = _REMOVE_DIR | _MAKE_DIR | _REFER
            permissions = [(candidate_fd, relocate), (publish_fd, relocate),
                           (registry_fd, _ALLOWED)]
        if audit_fd is not None:
            audit = os.fstat(audit_fd)
            named = os.stat('audit.bin', dir_fd=registry_fd, follow_symlinks=False)
            if (not stat.S_ISREG(audit.st_mode) or audit.st_nlink != 1
                    or audit.st_uid != os.geteuid() or stat.S_IMODE(audit.st_mode) != 0o600
                    or (audit.st_dev, audit.st_ino) != (named.st_dev, named.st_ino)
                    or audit.st_dev != identities[1]['dev']
                    or any(k.startswith('system.posix_acl_') for k in os.listxattr(audit_fd))):
                raise BootstrapRejected('IDENTITY_CHANGED')
            permissions = [(fd, mask) for fd, mask in permissions if fd != registry_fd]
            permissions.append((audit_fd, _WRITE_FILE))
        for fd, allowed in permissions:
            rule = _PathRule(allowed, fd)
            call(445, ctypes.c_int(ruleset), ctypes.c_int(1), ctypes.byref(rule), ctypes.c_uint(0))
        call(446, ctypes.c_int(ruleset), ctypes.c_uint(0))
        if [directory_identity(fd) for fd in (candidate_fd, registry_fd)] != identities:
            raise BootstrapRejected('IDENTITY_CHANGED')
        if publish_fd is not None and directory_identity(publish_fd) != publication_identity:
            raise BootstrapRejected('IDENTITY_CHANGED')
        return dict(landlock_abi=abi, handled_access_fs=_HANDLED,
                    role='publisher' if publish_fd is not None else 'creator',
                    rules=[dict(identity=directory_identity(fd), allowed_access_fs=allowed)
                           for fd, allowed in permissions])
    except BootstrapRejected:
        raise
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN') from exc
    finally:
        if ruleset is not None:
            os.close(ruleset)
