"""Deadline edge tests; no kernel hard-limit qualification."""
import pytest
from api import display_bootstrap_artifact as artifact


def test_expired_hash_does_not_start_read(monkeypatch):
    monkeypatch.setattr(artifact.os, 'lseek', lambda *args: 0)
    monkeypatch.setattr(artifact.time, 'monotonic', lambda: 10)
    def forbidden(*args):
        pytest.fail('expired hash started reading')
    monkeypatch.setattr(artifact.os, 'read', forbidden)
    with pytest.raises(ValueError, match='RESOURCE_LIMIT'):
        artifact._hash(-1, 10)


@pytest.mark.parametrize('block', [b'', b'data'])
def test_hash_read_crossing_deadline_is_rejected(monkeypatch, block):
    monkeypatch.setattr(artifact.os, 'lseek', lambda *args: 0)
    clock = iter([9, 10])
    monkeypatch.setattr(artifact.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(artifact.os, 'read', lambda *args: block)
    with pytest.raises(ValueError, match='RESOURCE_LIMIT'):
        artifact._hash(-1, 10)


def test_hash_before_deadline_reads_to_eof(monkeypatch):
    import hashlib
    monkeypatch.setattr(artifact.os, 'lseek', lambda *args: 0)
    monkeypatch.setattr(artifact.time, 'monotonic', lambda: 9)
    blocks = iter([b'data', b''])
    monkeypatch.setattr(artifact.os, 'read', lambda *args: next(blocks))
    assert artifact._hash(-1, 10) == hashlib.sha256(b'data').hexdigest()
