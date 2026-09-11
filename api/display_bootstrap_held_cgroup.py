"""Pinned original cgroup evidence for fail-closed batch termination."""
import os
import re
import stat
from api.display_bootstrap_audit_log import _deadline


class HeldCgroup:
    def __init__(self, root_fd, state, *, deadline):
        self.fd = None
        self.deadline = deadline
        _deadline(deadline)
        self.invocation = state.get('InvocationID')
        self.path = state.get('ControlGroup')
        if (type(self.invocation) is not str or re.fullmatch('[0-9a-f]{32}', self.invocation) is None
                or self.invocation == '0'*32 or type(self.path) is not str
                or re.fullmatch(r'/[A-Za-z0-9_.@:/-]+', self.path) is None):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        parts = self.path.split('/')[1:]
        if any(part in ('', '.', '..') for part in parts):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        current = os.dup(root_fd)
        try:
            for part in parts:
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                              dir_fd=current)
                os.close(current)
                current = new
            self.fd = os.open('cgroup.events', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                              dir_fd=current)
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
            self.identity = info.st_dev, info.st_ino
            _deadline(deadline)
        except BaseException:
            self.close()
            raise
        finally:
            os.close(current)

    def require_empty(self, state, *, original_job_finished):
        _deadline(self.deadline)
        if (self.fd is None or original_job_finished is not True
                or state.get('InvocationID') != self.invocation
                or state.get('ControlGroup') != self.path
                or state.get('ActiveState') not in ('inactive', 'failed')
                or state.get('SubState') not in ('dead', 'failed')
                or type(state.get('Job')) is not int or state['Job'] != 0):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        info = os.fstat(self.fd)
        if (info.st_dev, info.st_ino) != self.identity:
            raise ValueError('IDENTITY_CHANGED')
        raw = os.pread(self.fd, 4097, 0)
        if not raw or len(raw) > 4096:
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        rows = [line.split() for line in raw.decode('ascii', 'strict').splitlines()]
        if (any(len(row) != 2 for row in rows) or len({row[0] for row in rows}) != len(rows)
                or dict(rows).get('populated') != '0'):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        _deadline(self.deadline)

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)
