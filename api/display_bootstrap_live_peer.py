"""Live identity checks on preopened proc/cgroup handles, without subprocesses."""
import os
import select
from contextlib import ExitStack
from api.display_bootstrap_audit_log import _deadline


def _read(fd, maximum=16384):
    raw = os.pread(fd, maximum+1, 0)
    if not raw or len(raw) > maximum:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    return raw.decode('ascii', 'strict')


class LivePeer:
    """Launcher registers pidfd and protected read-only handles before confinement.

    unit_verify must query an already authenticated trusted control mechanism.
    This class does not replace PID1 unit validation with cgroup-file equality.
    """
    def __init__(self, *, peer, starttime, status_fd, stat_fd, cgroup_fd,
                 pidfd, expected_cgroup, groups, limits, deadline, unit_verify):
        self.peer, self.starttime = tuple(peer), starttime
        self.status_fd, self.stat_fd, self.cgroup_fd = status_fd, stat_fd, cgroup_fd
        self.expected_cgroup, self.groups = expected_cgroup, tuple(groups)
        self.limits, self.deadline = dict(limits), deadline
        self.unit_verify = unit_verify
        self.poller = select.poll()
        self.poller.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
        self.verify()

    def verify(self):
        _deadline(self.deadline)
        if self.poller.poll(0):
            raise ValueError('IDENTITY_CHANGED')
        stat = _read(self.stat_fd)
        if int(stat.rsplit(')', 1)[1].split()[19]) != self.starttime:
            raise ValueError('IDENTITY_CHANGED')
        status = dict(line.split(':', 1) for line in _read(self.status_fd).splitlines())
        pid, uid, gid = self.peer
        if (int(status['Pid']) != pid or tuple(map(int, status['Uid'].split())) != (uid,)*4
                or tuple(map(int, status['Gid'].split())) != (gid,)*4
                or tuple(sorted(map(int, status['Groups'].split()))) != self.groups
                or status['NoNewPrivs'].strip() != '1' or status['Seccomp'].strip() != '2'):
            raise ValueError('IDENTITY_CHANGED')
        if _read(self.cgroup_fd).splitlines() != ['0::'+self.expected_cgroup]:
            raise ValueError('IDENTITY_CHANGED')
        for fd, expected in self.limits.items():
            if _read(fd, 128).strip() != str(expected):
                raise ValueError('RESOURCE_LIMIT')
        self.unit_verify()
        if self.poller.poll(0):
            raise ValueError('IDENTITY_CHANGED')
        _deadline(self.deadline)
