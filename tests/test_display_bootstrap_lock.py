"""Real process lock contention and descriptor lifetime; not policy admission."""
import multiprocessing
import os
import pytest
from api import display_bootstrap_policy as policy


def contend(path, identity, queue):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            lock = policy.acquire_fixed_lock(fd, identity)
        except policy.BootstrapRejected as exc:
            queue.put(exc.code)
        else:
            lock.close()
            queue.put('ACQUIRED')
    finally:
        os.close(fd)


def test_lock_contention_and_release(tmp_path, monkeypatch):
    # spawn reimports this target in a fresh interpreter. The Agent also has
    # a tests package; keep this repository first in the serialized sys.path.
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    path = tmp_path / 'bootstrap.lock'
    path.touch(mode=0o600)
    st = path.stat()
    identity = {k: getattr(st, 'st_' + k) for k in ('dev','ino','uid','gid','mode','nlink')}
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        lock = policy.acquire_fixed_lock(fd, identity)
        context = multiprocessing.get_context('spawn')
        queue = context.Queue()
        child = context.Process(target=contend, args=(str(tmp_path), identity, queue))
        child.start()
        child.join(15)
        assert not child.is_alive()
        assert child.exitcode == 0
        assert queue.get(timeout=2) == 'LOCK_BUSY'
        lock.close()
        lock.close()
        replacement = policy.acquire_fixed_lock(fd, identity)
        replacement.close()
        queue.close()
        queue.join_thread()
        assert path.stat().st_ino == st.st_ino
    finally:
        os.close(fd)


def test_lock_identity_replacement(tmp_path):
    path = tmp_path / 'bootstrap.lock'
    path.touch(mode=0o600)
    st = path.stat()
    identity = {k: getattr(st, 'st_' + k) for k in ('dev','ino','uid','gid','mode','nlink')}
    path.rename(tmp_path / 'retained-original.lock')
    path.touch(mode=0o600)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(policy.BootstrapRejected, match='IDENTITY_CHANGED'):
            policy.acquire_fixed_lock(fd, identity)
    finally:
        os.close(fd)
