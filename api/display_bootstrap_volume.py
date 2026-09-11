"""Strict volume profile codec only; not a live quota verifier.

No admission authority is granted by parsing this record. Live mount, quota,
backing allocation and independent audit reserve checks are still required.
"""
import re
import os
import stat
from pathlib import Path


def observe_fixed_volume(directory_fd):
    """Read fixed loop-volume evidence, rejecting sparse backing files.

    This proves neither per-candidate quotas nor exclusive audit reserve.
    It deliberately grants no BootstrapContext admission authority.
    """
    from api.display_bootstrap_policy import open_protected_root, observe_platform
    parent = image = None
    try:
        platform = observe_platform(directory_fd)
        device = os.fstat(directory_fd).st_dev
        sysroot = Path(f'/sys/dev/block/{os.major(device)}:{os.minor(device)}')
        loop = sysroot / 'loop'
        backing = (loop / 'backing_file').read_text().strip()
        if not backing.startswith('/'):
            backing = '/' + backing
        if int((loop / 'offset').read_text()) != 0 or int((loop / 'sizelimit').read_text()) != 0:
            raise ValueError('offset or size limit')
        parent = open_protected_root(str(Path(backing).parent), {0})
        image = os.open(Path(backing).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=parent)
        info = os.fstat(image)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                or info.st_nlink != 1 or info.st_blocks * 512 < info.st_size
                or any(k.startswith('system.posix_acl_') for k in os.listxattr(image))):
            raise ValueError('unprotected or sparse backing')
        if int((sysroot / 'size').read_text()) * 512 != info.st_size:
            raise ValueError('device capacity mismatch')
        vfs = os.fstatvfs(directory_fd)
        named = os.stat(Path(backing).name, dir_fd=parent, follow_symlinks=False)
        after = os.fstat(image)
        fields = ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode',
                  'st_nlink', 'st_size', 'st_blocks')
        if any(getattr(info, field) != getattr(other, field)
               for other in (named, after) for field in fields):
            raise ValueError('backing identity changed')
        current_backing = (loop / 'backing_file').read_text().strip()
        if not current_backing.startswith('/'):
            current_backing = '/' + current_backing
        if (current_backing != backing or int((loop / 'offset').read_text()) != 0
                or int((loop / 'sizelimit').read_text()) != 0
                or int((sysroot / 'size').read_text()) * 512 != info.st_size
                or observe_platform(directory_fd) != platform):
            raise ValueError('volume changed during observation')
        return dict(device=device, mount_id=platform['mount_id'],
                    total_bytes=vfs.f_blocks*vfs.f_frsize, image_path=backing,
                    image_identity={k: getattr(info, 'st_'+k) for k in
                                    ('dev', 'ino', 'uid', 'gid', 'mode', 'nlink')},
                    image_bytes=info.st_size)
    except (OSError, ValueError) as exc:
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN') from exc
    finally:
        if image is not None:
            os.close(image)
        if parent is not None:
            os.close(parent)
from api.display_bootstrap_manifest import canonical_bytes, validate_record
from api.display_bootstrap_policy import BootstrapRejected


def validate_volume_profile(value, profile_id):
    try:
        canonical_bytes(value)
        if (type(profile_id) is not str or re.fullmatch('[0-9a-f]{32}', profile_id) is None
                or type(value) is not dict
                or set(value) != {'format_version', 'hard_limit_profile_id', 'candidate', 'audit'}
                or type(value['format_version']) is not int or value['format_version'] != 1
                or value['hard_limit_profile_id'] != profile_id):
            raise ValueError('INVALID_INPUT')
        for key in ('candidate', 'audit'):
            volume = value[key]
            if (type(volume) is not dict or set(volume) != {
                    'device', 'mount_id', 'total_bytes', 'image_path', 'image_identity', 'image_bytes'}):
                raise ValueError('INVALID_INPUT')
            for field in ('device', 'mount_id', 'total_bytes', 'image_bytes'):
                if type(volume[field]) is not int or volume[field] < 1:
                    raise ValueError('INVALID_INPUT')
            path = volume['image_path']
            if (type(path) is not str or not path.startswith('/') or '\x00' in path
                    or any(part in ('.', '..') for part in path.split('/'))):
                raise ValueError('INVALID_INPUT')
            validate_record(volume['image_identity'], 'file_identity')
            identity = volume['image_identity']
            if identity['uid'] != 0 or identity['mode'] & 0o022:
                raise ValueError('INVALID_INPUT')
            if volume['total_bytes'] > volume['image_bytes']:
                raise ValueError('INVALID_INPUT')
        if (value['candidate']['device'] == value['audit']['device']
                or value['candidate']['image_path'] == value['audit']['image_path']):
            raise ValueError('INVALID_INPUT')
        return value
    except ValueError as exc:
        raise BootstrapRejected('INVALID_INPUT') from exc


def read_volume_profile(profile_id):
    """Read only the fixed root-owned profile; never accept a caller path."""
    from api.display_bootstrap_policy import open_protected_root, read_protected_record
    from api.display_bootstrap_manifest import parse_record, digest
    if type(profile_id) is not str or re.fullmatch('[0-9a-f]{32}', profile_id) is None:
        raise BootstrapRejected('INVALID_INPUT')
    fd = open_protected_root('/etc/hermes-display-bootstrap/volumes', {0})
    try:
        raw = read_protected_record(fd, profile_id + '.json', {0})
        return validate_volume_profile(parse_record(raw), profile_id), digest(raw)
    except BootstrapRejected:
        raise
    except ValueError as exc:
        raise BootstrapRejected('INVALID_INPUT') from exc
    finally:
        os.close(fd)


def verify_fixed_volumes(roots, resource, *, profile_id=None):
    """Verify persisted capacity/identity, not audit metadata reservations.

    Admission still requires the separate audit and quota gates. Compare both
    ends of publication and re-read the protected profile to detect replacement.
    """
    if resource.get('format_version') == 3:
        if profile_id is None:
            raise BootstrapRejected('APPROVAL_MISMATCH')
    else:
        if profile_id is not None:
            raise BootstrapRejected('INVALID_INPUT')
        profile_id = resource['hard_limit_profile_id']
    profile, sha = read_volume_profile(profile_id)
    candidate_image = profile['candidate']['image_identity']
    audit_image = profile['audit']['image_identity']
    if (candidate_image['dev'], candidate_image['ino']) == (audit_image['dev'], audit_image['ino']):
        raise BootstrapRejected('IDENTITY_CHANGED')
    observed = {key: observe_fixed_volume(roots[key]) for key in
                ('candidate_root', 'publish_root', 'registry_root')}
    if (observed['candidate_root'] != profile['candidate']
            or observed['publish_root'] != profile['candidate']
            or observed['registry_root'] != profile['audit']):
        raise BootstrapRejected('IDENTITY_CHANGED')
    if read_volume_profile(profile_id) != (profile, sha):
        raise BootstrapRejected('IDENTITY_CHANGED')
    return dict(profile_sha=sha, candidate=observed['candidate_root'],
                audit=observed['registry_root'])
