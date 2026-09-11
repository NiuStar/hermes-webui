"""Pinned authenticated worker mount view; no setns or /proc/PID/root."""
import os
from contextlib import contextmanager
from api.display_bootstrap_audit_log import _deadline
from api.display_bootstrap_storage_topology import parse_mounts, mount_for_fd


def read_at(fd, name, deadline, maximum=1048576):
    _deadline(deadline)
    item = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    try:
        data = bytearray()
        while True:
            _deadline(deadline)
            chunk = os.read(item, min(65536, maximum + 1 - len(data)))
            if not chunk:
                return data.decode('utf-8', errors='strict')
            data.extend(chunk)
            if len(data) > maximum:
                raise ValueError('RESOURCE_LIMIT')
    finally:
        os.close(item)


def match_mount(mounts, path):
    matches = [m for m in mounts.values() if path == m['target']
               or path.startswith(m['target'].rstrip('/') + '/')]
    if not matches:
        raise ValueError('UNSUPPORTED_PLATFORM')
    length = max(len(m['target']) for m in matches)
    selected = [m for m in matches if len(m['target']) == length]
    if len(selected) != 1:
        raise ValueError('UNSUPPORTED_PLATFORM')
    mount = selected[0]
    if (mount['root'] != '/' or mount['filesystem'] != 'ext4'
            or 'rw' not in mount['options'] or 'ro' in mount['options']):
        raise ValueError('UNSUPPORTED_PLATFORM')
    return mount


def compare_views(mounts, path, fd, expected_id, device, options):
    target = match_mount(mounts, path)
    observer = mount_for_fd(fd, allow_readonly=True)
    if (target['mount_id'] != expected_id or target['device'] != device
            or observer['device'] != device or observer['target'] != target['target']
            or not set(options) <= set(target['options'])):
        raise ValueError('IDENTITY_CHANGED')
    return dict(target=target, observer=observer)


@contextmanager
def hold_target(peer, profile, deadline):
    # Caller must hold the authenticated worker pidfd for this entire scope.
    fd = os.open('/proc/%d' % peer[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        def snapshot():
            text = read_at(fd, 'stat', deadline, 65536)
            start = text[text.rfind(')') + 2:].split()[19]
            namespaces = {key: os.readlink('ns/' + name, dir_fd=fd)
                          for key, name in (('target_mount_namespace', 'mnt'),
                                            ('user_namespace', 'user'), ('cgroup_namespace', 'cgroup'))}
            if any(namespaces[k] != profile['namespace_constraints'][k] for k in namespaces):
                raise ValueError('APPROVAL_MISMATCH')
            return start, namespaces, parse_mounts(read_at(fd, 'mountinfo', deadline))
        before = snapshot()
        def verify():
            if snapshot() != before:
                raise ValueError('IDENTITY_CHANGED')
            _deadline(deadline)
        yield before[2], verify
        verify()
    finally:
        os.close(fd)
