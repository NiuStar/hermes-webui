"""Bounded, FD-relative inventory of application-owned growth roots.

No deletion, adoption, or accounting reset. The caller holds authority and
operation locks, supplies authenticated root FDs, and verifies semantic records.
"""
import os
import re
import stat
from api.display_bootstrap_v4 import checked

CID = re.compile('[0-9a-f]{32}')
CANDIDATE_FILES = {'display.sqlite', 'display.sqlite-wal', 'display.sqlite-shm', 'manifest.json'}


def scan_roots(roots, *, max_entries, check_deadline):
    expected = {'candidate_root', 'publish_root', 'registry_root', 'approval_root', 'ledger_root'}
    if type(roots) is not dict or set(roots) != expected:
        raise ValueError('INVALID_INPUT')
    if type(max_entries) is not int or not 1 <= max_entries <= 9007199254740991:
        raise ValueError('INVALID_INPUT')
    if not callable(check_deadline):
        raise ValueError('INVALID_INPUT')
    seen, filesystems, memberships = {}, {}, {k: set() for k in roots}
    objects = []
    walked = 0

    def account(info, owner, relative):
        nonlocal walked
        check_deadline()
        walked += 1
        if walked > max_entries:
            raise ValueError('RESOURCE_LIMIT')
        identity = info.st_dev, info.st_ino
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError('STATE_CONFLICT')
        if identity in seen:
            # Cross-root aliases are not extra quota capacity or independent CIDs.
            raise ValueError('STATE_CONFLICT')
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError('STATE_CONFLICT')
        seen[identity] = owner
        row = filesystems.setdefault(info.st_dev, {'bytes': 0, 'inodes': 0})
        row['bytes'] = checked(row['bytes'] + checked(info.st_blocks * 512))
        row['inodes'] = checked(row['inodes'] + 1)
        objects.append((owner, relative, identity, info.st_size, info.st_blocks))

    def unchanged(before, after):
        if any(getattr(before, k) != getattr(after, k) for k in (
                'st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode', 'st_nlink',
                'st_size', 'st_blocks', 'st_mtime_ns', 'st_ctime_ns')):
            raise ValueError('IDENTITY_CHANGED')

    for role, root in roots.items():
        baseline = os.fstat(root)
        if not stat.S_ISDIR(baseline.st_mode):
            raise ValueError('INVALID_INPUT')
        account(baseline, role, '')
        with os.scandir(root) as entries:
            for entry in entries:
                check_deadline()
                name = entry.name
                file_root = role in ('approval_root', 'ledger_root')
                cid = name[:-5] if file_root and name.endswith('.json') else name
                if CID.fullmatch(cid) is None or (file_root and name != cid + '.json'):
                    raise ValueError('STATE_CONFLICT')
                memberships[role].add(cid)
                before = os.stat(name, dir_fd=root, follow_symlinks=False)
                if before.st_dev != baseline.st_dev:
                    raise ValueError('UNSUPPORTED_PLATFORM')
                if file_root:
                    if not stat.S_ISREG(before.st_mode) or before.st_size > (
                            65536 if role == 'approval_root' else 32768):
                        raise ValueError('STATE_CONFLICT')
                    account(before, cid, role + '/' + name)
                else:
                    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 dir_fd=root)
                    try:
                        unchanged(before, os.fstat(fd))
                        account(before, cid, role + '/' + name)
                        allowed = {'audit.bin'} if role == 'registry_root' else CANDIDATE_FILES
                        with os.scandir(fd) as children:
                            for child in children:
                                if child.name not in allowed:
                                    raise ValueError('STATE_CONFLICT')
                                child_info = os.stat(child.name, dir_fd=fd, follow_symlinks=False)
                                if not stat.S_ISREG(child_info.st_mode) or child_info.st_dev != before.st_dev:
                                    raise ValueError('STATE_CONFLICT')
                                if child.name == 'manifest.json' and child_info.st_size > 65536:
                                    raise ValueError('RESOURCE_LIMIT')
                                if child.name == 'audit.bin' and child_info.st_size > 1183744:
                                    raise ValueError('RESOURCE_LIMIT')
                                account(child_info, cid, role + '/' + name + '/' + child.name)
                        unchanged(before, os.fstat(fd))
                    finally:
                        os.close(fd)
                unchanged(before, os.stat(name, dir_fd=root, follow_symlinks=False))
        unchanged(baseline, os.fstat(root))
    reserved = memberships['registry_root']
    if memberships['candidate_root'] & memberships['publish_root']:
        raise ValueError('STATE_CONFLICT')
    if any(not members <= reserved for role, members in memberships.items() if role != 'registry_root'):
        raise ValueError('STATE_CONFLICT')
    return {'candidate_ids': frozenset(reserved), 'memberships': memberships,
            'filesystems': filesystems, 'objects': tuple(objects)}
