"""One-request root storage observer; socket activation owns endpoint lifecycle.

Accepts only a fixed profile ID. Never writes devices, mounts filesystems,
returns FDs, creates sockets, or grants BootstrapContext authority.
"""
import fcntl
import os
import socket
import stat
import struct
import time

from api.display_bootstrap_journal import observe_device
from api.display_bootstrap_audit_log import _deadline


def _read_block(device, deadline):
    major, minor = device['major'], device['minor']
    if any(type(n) is not int or n < 0 for n in (major, minor)):
        raise ValueError('INVALID_INPUT')
    # /dev/block links are kernel/udev-managed; open the resolved device and
    # compare rdev. No client-supplied device name is used.
    path = '/dev/block/%d:%d' % (major, minor)
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISBLK(info.st_mode) or info.st_rdev != os.makedev(major, minor):
            raise ValueError('IDENTITY_CHANGED')
        size_raw = bytearray(8)
        fcntl.ioctl(fd, 0x80081272, size_raw, True)  # BLKGETSIZE64
        size, = struct.unpack('=Q', size_raw)
        result = observe_device(fd, size, deadline)
        if os.fstat(fd).st_rdev != info.st_rdev:
            raise ValueError('IDENTITY_CHANGED')
        result['device'] = device
        return result
    finally:
        os.close(fd)


def observe_profile(profile_id, deadline):
    from api.display_bootstrap_volume import read_volume_profile
    from api.display_bootstrap_policy import open_protected_root
    from api.display_bootstrap_storage_topology import mount_for_fd, direct_disk
    profile, sha = read_volume_profile(profile_id)
    audit = profile['audit']
    identity = audit['image_identity']
    parent = image = None
    from pathlib import Path
    try:
        parent = open_protected_root(str(Path(audit['image_path']).parent), {0})
        image = os.open(Path(audit['image_path']).name, os.O_RDONLY | os.O_NOFOLLOW
                        | os.O_NONBLOCK, dir_fd=parent)
        info = os.fstat(image)
        if ({k: getattr(info, 'st_' + k) for k in identity} != identity
                or info.st_size != audit['image_bytes']):
            raise ValueError('IDENTITY_CHANGED')
        host = mount_for_fd(image)
        direct_disk(host['device'])
        loop_device = dict(major=os.major(audit['device']), minor=os.minor(audit['device']))
        result = dict(loop=_read_block(loop_device, deadline), host=_read_block(host['device'], deadline))
        if read_volume_profile(profile_id) != (profile, sha):
            raise ValueError('IDENTITY_CHANGED')
        return result, sha
    finally:
        try:
            if image is not None:
                os.close(image)
        finally:
            if parent is not None:
                os.close(parent)


def observe_bound(binding, peer, deadline):
    from pathlib import Path
    from api.display_bootstrap_storage_worker import verify_worker_binding
    from api.display_bootstrap_observer_release import read_observer
    from api.display_bootstrap_observer_self import observe_self
    from api.display_bootstrap_storage_target import hold_target, compare_views
    from api.display_bootstrap_storage import _read
    from api.display_bootstrap_volume import read_volume_profile
    from api.display_bootstrap_policy import open_protected_root, directory_identity
    from api.display_bootstrap_extents import verify_allocated
    from api.display_bootstrap_storage_topology import direct_disk
    config, _, _ = verify_worker_binding(peer, binding, deadline)
    profile, release = read_observer(binding['observer_profile_id'], binding['observer_profile_sha'], deadline)
    self_before = observe_self(profile, deadline)
    contract, contract_sha = _read('/etc/hermes-display-bootstrap/storage-contracts', config['storage_contract_id'])
    volume, volume_sha = read_volume_profile(binding['volume_profile_id'])
    if contract_sha != config['storage_contract_sha'] or volume_sha != binding['volume_profile_sha']:
        raise ValueError('APPROVAL_MISMATCH')
    topology = contract['audit_topology']
    registry = parent = image = None
    try:
        root = config['roots']['registry_root']
        registry = open_protected_root(root['path'], {0, config['creator_uid']})
        if directory_identity(registry) != root['identity']:
            raise ValueError('IDENTITY_CHANGED')
        device = topology['loop_device']
        loop = Path('/sys/dev/block/%d:%d/loop' % (device['major'], device['minor']))
        def loop_binding():
            return ((loop / 'backing_file').read_text().rstrip('\n'),
                    (loop / 'offset').read_text().strip(), (loop / 'sizelimit').read_text().strip())
        backing = loop_binding()
        if backing != (volume['audit']['image_path'], '0', '0'):
            raise ValueError('IDENTITY_CHANGED')
        parent = open_protected_root(str(Path(backing[0]).parent), {0})
        image = os.open(Path(backing[0]).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        before = os.fstat(image)
        if ({k: getattr(before, 'st_' + k) for k in topology['backing_identity']} != topology['backing_identity']
                or before.st_size != topology['backing_bytes'] or os.listxattr(image)):
            raise ValueError('IDENTITY_CHANGED')
        verify_allocated(image, before.st_size, deadline)
        with hold_target(peer, profile, deadline) as (mounts, verify_target):
            def views():
                return dict(loop=compare_views(mounts, root['path'], registry, topology['loop_mount_id'],
                                               device, topology['required_mount_options']),
                            host=compare_views(mounts, backing[0], image, topology['host_mount_id'],
                                               topology['host_device'], ['rw']))
            view = views()
            disk = direct_disk(topology['host_device'])
            if any(disk[k] != topology[k] for k in disk):
                raise ValueError('IDENTITY_CHANGED')
            result = dict(loop=_read_block(device, deadline), host=_read_block(topology['host_device'], deadline),
                          views=view, observer_self=self_before)
            verify_target()
            named = os.stat(Path(backing[0]).name, dir_fd=parent, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
            if (views() != view or loop_binding() != backing
                    or any(getattr(before, k) != getattr(other, k) for k in fields for other in (named, os.fstat(image)))
                    or observe_self(profile, deadline) != self_before
                    or read_observer(binding['observer_profile_id'], binding['observer_profile_sha'], deadline) != (profile, release)
                    or read_volume_profile(binding['volume_profile_id']) != (volume, volume_sha)):
                raise ValueError('IDENTITY_CHANGED')
            verify_worker_binding(peer, binding, deadline)
            return result
    finally:
        for fd in (image, parent, registry):
            if fd is not None:
                os.close(fd)


def handle(connection, *, timeout_seconds=10):
    if os.geteuid() != 0 or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 15:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    deadline = time.monotonic() + timeout_seconds
    if connection.getsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED) != 1:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    from api.display_bootstrap_storage_handshake import server
    from api.display_bootstrap_storage_worker import hold_worker
    server(connection, deadline=deadline, hold_worker=hold_worker, observe=observe_bound)
    connection.shutdown(socket.SHUT_WR)
