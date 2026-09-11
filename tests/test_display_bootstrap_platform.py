"""Real Linux primitives on retained, isolated test paths."""
import importlib.util
import os

import pytest


@pytest.mark.parametrize('name', ['../outside', '/absolute', '.', '..', 'a/b', 'a\x00b', ''])
def test_no_replace_rejects_non_leaf_names(tmp_path, name):
    from api.display_bootstrap_publish import rename_no_replace
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError):
            rename_no_replace(fd, name, fd, 'target')
        with pytest.raises(ValueError):
            rename_no_replace(fd, 'source', fd, name)
        assert list(tmp_path.iterdir()) == []
    finally:
        os.close(fd)


def test_atomic_record_no_overwrite(tmp_path):
    import api.display_bootstrap_publish as publisher
    assert hasattr(publisher, 'write_record'), 'durable record writer missing'
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        raw = b'{"a":1}'
        publisher.write_record(fd, 'record.json', raw)
        assert (tmp_path / 'record.json').read_bytes() == raw
        with pytest.raises(FileExistsError):
            publisher.write_record(fd, 'record.json', b'{"a":2}')
        assert (tmp_path / 'record.json').read_bytes() == raw
        assert len(list(tmp_path.iterdir())) == 2  # failed temp retained
    finally:
        os.close(fd)


@pytest.mark.parametrize('stage', ['file_sync', 'rename', 'directory_sync'])
def test_record_sync_failure_retains_evidence(tmp_path, monkeypatch, stage):
    import api.display_bootstrap_publish as publisher
    import errno
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_sync = os.fsync
    def sync(target):
        if (target == fd) == (stage == 'directory_sync'):
            raise OSError(errno.EIO, 'injected sync failure')
        return original_sync(target)
    def fail_rename(*args):
        raise OSError(errno.EIO, 'injected rename failure')
    try:
        if stage == 'rename':
            monkeypatch.setattr(publisher, 'rename_no_replace', fail_rename)
        else:
            monkeypatch.setattr(publisher.os, 'fsync', sync)
        with pytest.raises(OSError):
            publisher.write_record(fd, 'record.json', b'{"a":1}')
        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert files[0].read_bytes() == b'{"a":1}'
        assert (files[0].name == 'record.json') == (stage == 'directory_sync')
    finally:
        os.close(fd)


def test_publish_no_replace(tmp_path):
    assert importlib.util.find_spec('api.display_bootstrap_publish'), 'no-replace implementation missing'
    from api.display_bootstrap_publish import rename_no_replace
    source = tmp_path / 'source'
    target = tmp_path / 'target'
    source.mkdir()
    target.mkdir()
    (target / 'sentinel').write_bytes(b'keep')
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(FileExistsError):
            rename_no_replace(fd, 'source', fd, 'target')
        assert source.is_dir()
        assert (target / 'sentinel').read_bytes() == b'keep'
        identity = source.stat().st_ino
        rename_no_replace(fd, 'source', fd, 'new')
        assert not source.exists()
        assert (tmp_path / 'new').stat().st_ino == identity
    finally:
        os.close(fd)
