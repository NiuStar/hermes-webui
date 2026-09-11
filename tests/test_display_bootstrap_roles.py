"""Role checks with explicit process evidence; not live runner qualification."""
from pathlib import Path
import pytest
from api import display_bootstrap_roles as roles


def configure(monkeypatch, role='creator', extra=False):
    config = dict(creator_uid=1001, creator_gid=1001, approver_uid=1002,
                  approver_gid=1002, approval_read_gid=1001)
    uid = 1002 if role == 'approver' else 1001
    gid = uid
    monkeypatch.setattr(roles.os, 'getresuid', lambda: (uid,) * 3)
    monkeypatch.setattr(roles.os, 'getresgid', lambda: (gid,) * 3)
    groups = ([1001] if role == 'approver' else []) + ([9999] if extra else [])
    monkeypatch.setattr(roles.os, 'getgroups', lambda: groups)
    monkeypatch.setattr(roles.os, 'readlink', lambda path: 'user:[4026531837]')
    status = f'Uid:\t{uid} {uid} {uid} {uid}\nGid:\t{gid} {gid} {gid} {gid}\n'
    status += ''.join(k + ':\t0\n' for k in ('CapInh','CapPrm','CapEff','CapBnd','CapAmb'))
    status += 'NoNewPrivs:\t1\nThreads:\t1\n'
    monkeypatch.setattr(Path, 'read_text', lambda self:
                        '0 0 4294967295\n' if self.name in ('uid_map', 'gid_map') else status)
    return config


@pytest.mark.parametrize('role', ['creator', 'publisher', 'recover', 'approver'])
def test_role_ids_and_groups(monkeypatch, role):
    config = configure(monkeypatch, role)
    assert roles.verify_identity(config, role)['role'] == role


@pytest.mark.parametrize('role', ['creator', 'approver'])
def test_extra_group_rejected(monkeypatch, role):
    config = configure(monkeypatch, role, True)
    with pytest.raises(ValueError, match='ACCESS_BOUNDARY_UNPROVEN'):
        roles.verify_identity(config, role)


def test_saved_uid_rejected(monkeypatch):
    config = configure(monkeypatch)
    monkeypatch.setattr(roles.os, 'getresuid', lambda: (1001, 1001, 0))
    with pytest.raises(ValueError):
        roles.verify_identity(config, 'creator')


def test_namespace_remapping_rejected(monkeypatch):
    config = configure(monkeypatch)
    original = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda self:
                        '0 100000 65536\n' if self.name == 'uid_map' else original(self))
    with pytest.raises(ValueError, match='ACCESS_BOUNDARY_UNPROVEN'):
        roles.verify_identity(config, 'creator')


def test_creator_cannot_be_approver(monkeypatch):
    config = configure(monkeypatch)
    with pytest.raises(ValueError):
        roles.verify_identity(config, 'approver')
