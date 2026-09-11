"""Explicit creator/approver process and DAC identities for V2 policies."""
import os
from pathlib import Path
import stat


def verify_identity(config, role):
    if role not in ('creator', 'publisher', 'recover', 'approver'):
        raise ValueError('INVALID_INPUT')
    selected = 'approver' if role == 'approver' else 'creator'
    uid, gid = config[selected + '_uid'], config[selected + '_gid']
    values = [config[k] for k in ('creator_uid', 'approver_uid', 'creator_gid',
                                 'approver_gid', 'approval_read_gid')]
    if (any(type(v) is not int or v <= 0 for v in values)
            or config['creator_uid'] == config['approver_uid']
            or config['creator_gid'] == config['approver_gid']
            or config['approval_read_gid'] != config['creator_gid']):
        raise ValueError('INVALID_INPUT')
    expected_groups = {config['approval_read_gid']} if selected == 'approver' else set()
    groups = set(os.getgroups()) - {gid}
    if (os.getresuid() != (uid,) * 3 or os.getresgid() != (gid,) * 3
            or groups != expected_groups):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    status = {}
    for line in Path('/proc/self/status').read_text().splitlines():
        if ':' in line:
            key, value = line.split(':', 1)
            if key in status:
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
            status[key] = value.strip()
    if (list(map(int, status['Uid'].split())) != [uid] * 4
            or list(map(int, status['Gid'].split())) != [gid] * 4
            or any(int(status[k], 16) != 0 for k in
                   ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'))
            or status['NoNewPrivs'] != '1'
            or status['Threads'] != '1'):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    for name in ('uid_map', 'gid_map'):
        if Path('/proc/self/' + name).read_text().split() != ['0', '0', '4294967295']:
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    return dict(role=role, uid=uid, gid=gid, supplementary_gids=sorted(groups),
                pid=os.getpid(), user_namespace=os.readlink('/proc/self/ns/user'))


def verify_dac_configuration(roots, config, *, role, before_confinement):
    """Static DAC checks remain meaningful after irreversible Landlock denial."""
    verify_identity(config, role)
    expected = {'approval_root', 'lock_root'} if role == 'approver' else {
        'candidate_root', 'publish_root', 'registry_root', 'approval_root', 'lock_root'}
    if set(roots) != expected:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    identities = set()
    for key, fd in roots.items():
        info = os.fstat(fd)
        identity = {k: getattr(info, 'st_' + k) for k in ('dev', 'ino', 'uid', 'gid', 'mode')}
        if identity != config['roots'][key]['identity']:
            raise ValueError('IDENTITY_CHANGED')
        identities.add((info.st_dev, info.st_ino))
        if key == 'approval_root':
            verify_approval_root(fd, config, role=role, before_confinement=before_confinement)
            continue
        owner = 0 if key == 'lock_root' else config['creator_uid']
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != owner
                or info.st_mode & 0o022
                or (key != 'lock_root' and info.st_gid != config['creator_gid'])
                or any(k.startswith('system.posix_acl_') for k in os.listxattr(fd))):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        if before_confinement:
            writable = key != 'lock_root' and role != 'approver'
            if (not os.access('.', os.R_OK | os.X_OK, dir_fd=fd, effective_ids=True)
                    or os.access('.', os.W_OK, dir_fd=fd, effective_ids=True) != writable):
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    if len(identities) != len(expected):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')


def verify_approval_root(fd, config, *, role, before_confinement):
    verify_identity(config, role)
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != config['approver_uid']
            or info.st_gid != config['approval_read_gid']
            or stat.S_IMODE(info.st_mode) != 0o2750
            or any(k.startswith('system.posix_acl_') for k in os.listxattr(fd))):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    if before_confinement:
        if (not os.access('.', os.R_OK | os.X_OK, dir_fd=fd, effective_ids=True)
                or os.access('.', os.W_OK, dir_fd=fd, effective_ids=True) != (role == 'approver')):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    return {k: getattr(info, 'st_' + k) for k in ('dev', 'ino', 'uid', 'gid', 'mode')}
