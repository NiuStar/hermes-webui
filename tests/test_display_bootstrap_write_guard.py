"""Descriptor scanner unit tests; no Landlock is installed in pytest."""
import errno
from pathlib import Path
import pytest
from api import display_bootstrap_write_guard as guard


@pytest.mark.parametrize('code', [errno.EBADF, errno.ENOENT])
def test_closed_proc_iterator_descriptor_is_ignored(monkeypatch, code):
    monkeypatch.setattr(Path, 'iterdir', lambda _: iter([Path('/proc/self/fd/999')]))
    monkeypatch.setattr(Path, 'read_text', lambda _: '')
    def closed(_):
        raise OSError(code, 'closed')
    monkeypatch.setattr(guard.os, 'fstat', closed)
    guard._check_descriptors()


def test_other_descriptor_errors_are_not_ignored(monkeypatch):
    monkeypatch.setattr(Path, 'iterdir', lambda _: iter([Path('/proc/self/fd/999')]))
    def denied(_):
        raise OSError(errno.EACCES, 'denied')
    monkeypatch.setattr(guard.os, 'fstat', denied)
    with pytest.raises(OSError) as caught:
        guard._check_descriptors()
    assert caught.value.errno == errno.EACCES
