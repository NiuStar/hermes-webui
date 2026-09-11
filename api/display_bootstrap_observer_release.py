"""Read pinned observer/release files without following user-writable paths."""
import hashlib
import os
from pathlib import PurePosixPath
from api.display_bootstrap_audit_log import _deadline
from api.display_bootstrap_manifest import parse_record, digest
from api.display_bootstrap_policy import open_protected_root, read_protected_record
from api.display_bootstrap_observer_profile import validate_profile, validate_release
from api.display_bootstrap_v3 import hex_value


def verify_file(path, expected, sha, deadline):
    parent = fd = None
    try:
        _deadline(deadline)
        parent = open_protected_root(str(PurePosixPath(path).parent), {0})
        fd = os.open(PurePosixPath(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=parent)
        before = os.fstat(fd)
        if {k:getattr(before,'st_'+k) for k in expected} != expected or os.listxattr(fd):
            raise ValueError('IDENTITY_CHANGED')
        h=hashlib.sha256()
        offset=0
        while offset < before.st_size:
            _deadline(deadline)
            chunk=os.pread(fd,min(65536,before.st_size-offset),offset)
            if not chunk:
                raise ValueError('IDENTITY_CHANGED')
            h.update(chunk)
            offset+=len(chunk)
        named=os.stat(PurePosixPath(path).name,dir_fd=parent,follow_symlinks=False)
        after=os.fstat(fd)
        fields=('st_dev','st_ino','st_uid','st_gid','st_mode','st_nlink','st_size','st_mtime_ns','st_ctime_ns')
        if h.hexdigest()!=sha or any(getattr(before,k)!=getattr(other,k) for k in fields for other in (named,after)):
            raise ValueError('IDENTITY_CHANGED')
        _deadline(deadline)
    finally:
        if fd is not None: os.close(fd)
        if parent is not None: os.close(parent)


def read_observer(profile_id, profile_sha, deadline):
    hex_value(profile_id,32)
    hex_value(profile_sha,64)
    root=open_protected_root('/etc/hermes-display-bootstrap/observers',{0})
    try:
        raw=read_protected_record(root,profile_id+'.json',{0})
    finally:
        os.close(root)
    profile=validate_profile(parse_record(raw))
    if digest(raw)!=profile_sha or profile['observer_profile_id']!=profile_id:
        raise ValueError('APPROVAL_MISMATCH')
    root=open_protected_root('/etc/hermes-display-bootstrap/releases',{0})
    try:
        raw=read_protected_record(root,profile['release_manifest_sha']+'.json',{0})
    finally:
        os.close(root)
    if digest(raw)!=profile['release_manifest_sha']:
        raise ValueError('APPROVAL_MISMATCH')
    release=validate_release(parse_record(raw))
    executable=profile['python_executable_identity']
    verify_file(executable['path'],executable['identity'],executable['sha'],deadline)
    from pathlib import Path
    covered = {item['path'] for item in release['files']}
    required = {'scripts/bootstrap_storage_entry.py',
                'hermes-bootstrap-storage@.service', 'hermes-bootstrap-storage.socket'}
    # Every importable project source must be pinned, including api/__init__.py.
    # Reject symlink directories before recursion rather than following them.
    pending = [Path(release['release_root']) / 'api']
    import stat
    while pending:
        _deadline(deadline)
        directory = pending.pop()
        protected = open_protected_root(str(directory), {0})
        try:
            with os.scandir(protected) as entries:
                for entry in entries:
                    _deadline(deadline)
                    info = entry.stat(follow_symlinks=False)
                    relative = str((directory / entry.name).relative_to(release['release_root']))
                    if stat.S_ISDIR(info.st_mode):
                        if entry.name == '__pycache__':
                            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
                        pending.append(directory / entry.name)
                    elif stat.S_ISREG(info.st_mode):
                        required.add(relative)
                    else:
                        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
                    if len(required) + len(pending) > 256:
                        raise ValueError('RESOURCE_LIMIT')
        finally:
            os.close(protected)
    if not required <= covered:
        raise ValueError('APPROVAL_MISMATCH')
    for item in release['files']:
        verify_file(release['release_root']+'/'+item['path'],item['identity'],item['sha'],deadline)
    return profile,release
