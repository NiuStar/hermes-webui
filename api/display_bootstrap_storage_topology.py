"""Read-only mount/device topology. Not a storage acceptance certificate."""
import os
from pathlib import Path
import re

from api.display_bootstrap_audit_log import _deadline
from api.display_bootstrap_extents import verify_allocated


def _unescape(value):
    # Only mountinfo's documented escaping is accepted.
    allowed = {'040': ' ', '011': '\t', '012': '\n', '134': '\\'}
    def replace(match):
        if match[1] not in allowed:
            raise ValueError('UNSUPPORTED_PLATFORM')
        return allowed[match[1]]
    if re.search(r'\\(?![0-7]{3})', value):
        raise ValueError('UNSUPPORTED_PLATFORM')
    return re.sub(r'\\([0-7]{3})', replace, value)


def parse_mounts(text):
    result = {}
    for line in text.splitlines():
        fields = line.split()
        try:
            divider = fields.index('-')
            if divider < 6 or len(fields) != divider + 4:
                raise ValueError('UNSUPPORTED_PLATFORM')
            mount_id = int(fields[0])
            major, minor = map(int, fields[2].split(':'))
            if mount_id <= 0 or mount_id in result or min(major, minor) < 0:
                raise ValueError('UNSUPPORTED_PLATFORM')
            result[mount_id] = dict(mount_id=mount_id, device={'major': major, 'minor': minor},
                                   root=_unescape(fields[3]), target=_unescape(fields[4]),
                                   filesystem=fields[divider + 1],
                                   options=sorted(set(fields[5].split(',') +
                                                      fields[divider + 3].split(','))))
        except (IndexError, ValueError) as exc:
            raise ValueError('UNSUPPORTED_PLATFORM') from exc
    return result


def mount_for_fd(fd, *, allow_readonly=False):
    metadata = Path('/proc/self/fdinfo', str(fd)).read_text()
    ids = [line.split(':', 1)[1].strip() for line in metadata.splitlines()
           if line.startswith('mnt_id:')]
    if len(ids) != 1 or not ids[0].isdigit():
        raise ValueError('UNSUPPORTED_PLATFORM')
    mount = parse_mounts(Path('/proc/self/mountinfo').read_text()).get(int(ids[0]))
    info = os.fstat(fd)
    if (mount is None or mount['root'] != '/' or mount['filesystem'] != 'ext4'
            or (not allow_readonly and ('rw' not in mount['options'] or 'ro' in mount['options']))
            or mount['device'] != dict(major=os.major(info.st_dev), minor=os.minor(info.st_dev))):
        raise ValueError('UNSUPPORTED_PLATFORM')
    return mount


def direct_disk(device):
    """Allow a direct disk or one ordinary partition; refuse stacked devices."""
    path = Path('/sys/dev/block', '%d:%d' % (device['major'], device['minor'])).resolve(strict=True)
    partition = (path / 'partition').exists()
    start = int((path / 'start').read_text()) if partition else 0
    disk = path.parent if partition else path
    if (start < 0 or (disk / 'partition').exists()
            or disk.name.startswith(('loop', 'dm-', 'md', 'ram', 'zram'))):
        raise ValueError('UNSUPPORTED_PLATFORM')
    for node in {path, disk}:
        slaves = node / 'slaves'
        # Partitions inherit the parent disk's dependency topology.
        if ((node / 'dm').exists() or (node / 'md').exists() or (node / 'loop').exists()
                or (slaves.exists() and any(slaves.iterdir()))
                or (node == disk and not slaves.is_dir())):
            raise ValueError('UNSUPPORTED_PLATFORM')
    # A kernel device node is necessary: unknown virtual stack types fail closed.
    if not (disk / 'device').exists():
        raise ValueError('UNSUPPORTED_PLATFORM')
    major, minor = map(int, (disk / 'dev').read_text().strip().split(':'))
    return dict(host_disk_device={'major': major, 'minor': minor},
                host_partition_start_sectors=start)


def observe(directory_fd, *, deadline):
    from api.display_bootstrap_policy import open_protected_root
    _deadline(deadline)
    volume = mount_for_fd(directory_fd)
    dev = volume['device']
    loop = Path('/sys/dev/block', '%d:%d' % (dev['major'], dev['minor']), 'loop')
    def binding():
        backing = (loop / 'backing_file').read_text().rstrip('\n')
        # Do not repair relative/malformed kernel paths into accepted paths.
        if not backing.startswith('/') or '\0' in backing:
            raise ValueError('UNSUPPORTED_PLATFORM')
        if int((loop / 'offset').read_text()) or int((loop / 'sizelimit').read_text()):
            raise ValueError('UNSUPPORTED_PLATFORM')
        return backing
    backing = binding()
    parent = image = None
    try:
        parent = open_protected_root(str(Path(backing).parent), {0})
        image = os.open(Path(backing).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=parent)
        before = os.fstat(image)
        if before.st_uid != 0 or before.st_mode & 0o022 or os.listxattr(image):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        host = mount_for_fd(image)
        disk = direct_disk(host['device'])
        verify_allocated(image, before.st_size, deadline)
        after = os.fstat(image)
        named = os.stat(Path(backing).name, dir_fd=parent, follow_symlinks=False)
        fields = ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode', 'st_nlink',
                  'st_size', 'st_blocks', 'st_mtime_ns', 'st_ctime_ns')
        if (any(getattr(before, k) != getattr(other, k) for k in fields for other in (after, named))
                or binding() != backing or mount_for_fd(image) != host
                or mount_for_fd(directory_fd) != volume):
            raise ValueError('IDENTITY_CHANGED')
        sectors = int((loop.parent / 'size').read_text())
        if sectors * 512 != before.st_size:
            raise ValueError('IDENTITY_CHANGED')
        _deadline(deadline)
        return dict(volume=volume, host=host, backing_bytes=before.st_size,
                    backing_identity={k: getattr(before, 'st_' + k) for k in
                                      ('dev', 'ino', 'uid', 'gid', 'mode', 'nlink')}, **disk)
    finally:
        try:
            if image is not None:
                os.close(image)
        finally:
            if parent is not None:
                os.close(parent)
