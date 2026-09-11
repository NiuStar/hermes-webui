"""Held root capacity sampling and same-time-namespace verification."""
import os
import time
from api.display_bootstrap_policy import directory_identity
from api.display_bootstrap_v4 import checked
from api.display_bootstrap_audit_log import _deadline


def capacity(fd, expected):
    """expected is launcher authority, never values from an OBSERVE request."""
    if directory_identity(fd) != expected['root_identity']:
        raise ValueError('IDENTITY_CHANGED')
    info = os.fstat(fd)
    if expected['device'] != dict(major=os.major(info.st_dev), minor=os.minor(info.st_dev)):
        raise ValueError('IDENTITY_CHANGED')
    values = os.fstatvfs(fd)
    if values.f_frsize != 4096 or values.f_bsize != 4096:
        raise ValueError('UNSUPPORTED_PLATFORM')
    free_bytes = checked(checked(values.f_bavail) * 4096)
    free_inodes = checked(values.f_favail)
    if directory_identity(fd) != expected['root_identity']:
        raise ValueError('IDENTITY_CHANGED')
    return dict(expected, free_bytes=free_bytes, free_inodes=free_inodes)


class HeldSampler:
    """Borrows roots from the session owner; revalidates complete backing each round."""
    def __init__(self, roots, expected, *, observe_storage, verify, deadline):
        if set(roots) != {'host', 'audit'} or set(expected) != set(roots):
            raise ValueError('INVALID_INPUT')
        self.roots = dict(roots)
        self.expected = {k: dict(v) for k, v in expected.items()}
        for row in self.expected.values():
            if set(row) != {'fs_uuid', 'device', 'root_identity'}:
                raise ValueError('INVALID_INPUT')
        self.observe_storage = observe_storage
        self.verify = verify
        self.deadline = deadline

    def __call__(self):
        _deadline(self.deadline)
        started = checked(time.monotonic_ns())
        self.verify()
        storage = self.observe_storage()
        values = {name: capacity(fd, self.expected[name]) for name, fd in self.roots.items()}
        self.verify()
        finished = checked(time.monotonic_ns())
        _deadline(self.deadline)
        return dict(format_version=1, started_ns=started, finished_ns=finished,
                    storage=storage, capacity=values)


class TimeDomain:
    """Borrows a launcher-held host namespace FD; no permissive missing-proc fallback."""
    def __init__(self, host_time_fd, pids):
        self.fd = host_time_fd
        self.pids = tuple(pids)
        if not self.pids or any(type(pid) is not int or pid <= 0 for pid in self.pids):
            raise ValueError('INVALID_INPUT')
        info = os.fstat(host_time_fd)
        self.identity = info.st_dev, info.st_ino
        self.verify()

    def verify(self):
        info = os.fstat(self.fd)
        if (info.st_dev, info.st_ino) != self.identity:
            raise ValueError('IDENTITY_CHANGED')
        for pid in self.pids:
            for name in ('time', 'time_for_children'):
                info = os.stat('/proc/%d/ns/%s' % (pid, name))
                if (info.st_dev, info.st_ino) != self.identity:
                    raise ValueError('IDENTITY_CHANGED')
