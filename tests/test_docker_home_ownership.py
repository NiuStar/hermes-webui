from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INIT = (ROOT / "docker_init.bash").read_text(encoding="utf-8")


def test_docker_init_delegates_home_ownership_to_fd_safe_helper():
    assert "/apptoo/api/docker_home_ownership.py" in INIT
    assert "os.walk(home" not in INIT


def test_fd_walker_skips_nested_mount_without_chowning_mount_root(tmp_path, monkeypatch):
    from api import docker_home_ownership as ownership

    home = tmp_path / "home"
    nested = home / "nested"
    nested.mkdir(parents=True)
    (nested / "sentinel").write_text("x", encoding="utf-8")
    nested_inode = nested.stat().st_ino
    touched = []

    original_mount_id = ownership._mount_id

    def fake_mount_id(fd):
        if __import__("os").fstat(fd).st_ino == nested_inode:
            return original_mount_id(fd) + 1
        return original_mount_id(fd)

    monkeypatch.setattr(ownership, "_mount_id", fake_mount_id)
    monkeypatch.setattr(
        ownership,
        "_chown_fd",
        lambda fd, _uid, _gid: touched.append(__import__("os").fstat(fd).st_ino),
    )

    ownership.chown_home(home, 501, 20)

    assert nested_inode not in touched
    assert (nested / "sentinel").stat().st_ino not in touched


def test_fd_walker_never_follows_symlink_target(tmp_path, monkeypatch):
    from api import docker_home_ownership as ownership

    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    target = outside / "sentinel"
    target.write_text("x", encoding="utf-8")
    link = home / "link"
    link.symlink_to(target)
    target_inode = target.stat().st_ino
    link_inode = link.lstat().st_ino
    touched = []

    monkeypatch.setattr(
        ownership,
        "_chown_fd",
        lambda fd, _uid, _gid: touched.append(__import__("os").fstat(fd).st_ino),
    )
    ownership.chown_home(home, 501, 20)

    assert link_inode in touched
    assert target_inode not in touched


def test_fd_walker_uses_fd_relative_nofollow_operations_only():
    source = (ROOT / "api" / "docker_home_ownership.py").read_text(encoding="utf-8")
    assert "dir_fd=" in source
    assert "os.O_NOFOLLOW" in source
    assert "AT_EMPTY_PATH" in source
    assert "/proc/self/fdinfo/" in source
    assert "os.chown(" not in source
    assert "os.lchown(" not in source


def test_fd_walker_propagates_non_enoent_open_errors(tmp_path, monkeypatch):
    from api import docker_home_ownership as ownership

    home = tmp_path / "home"
    home.mkdir()
    (home / "blocked").write_text("x", encoding="utf-8")

    monkeypatch.setattr(ownership, "_chown_fd", lambda _fd, _uid, _gid: None)
    monkeypatch.setattr(
        ownership,
        "_open_directory",
        lambda _parent_fd, _name: (_ for _ in ()).throw(PermissionError("blocked")),
    )
    with pytest.raises(PermissionError, match="blocked"):
        ownership.chown_home(home, 501, 20)


@pytest.mark.parametrize("uid,gid", [(-1, 20), (501, -1), (2**32 - 1, 20), (501, 2**32 - 1)])
def test_fd_walker_rejects_unsafe_chown_ids(tmp_path, uid, gid):
    from api import docker_home_ownership as ownership

    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(ValueError, match="outside supported chown range"):
        ownership.chown_home(home, uid, gid)
