"""FIEMAP response protocol tests; mocks are not live storage evidence."""
import time

import pytest

from api import display_bootstrap_extents as ext


def response(monkeypatch, *, flags=1, logical=0, physical=4096, length=8192,
             count=1, reserved=0):
    def ioctl(fd, command, buffer, mutate):
        assert command == ext._FIEMAP
        start, size, _, _, capacity, _ = ext._HEADER.unpack_from(buffer)
        ext._HEADER.pack_into(buffer, 0, start, size, 1, count, capacity, reserved)
        ext._EXTENT.pack_into(buffer, ext._HEADER.size,
                              logical, physical, length, 0, 0, flags, 0, 0, 0)
        return 0
    monkeypatch.setattr(ext.fcntl, 'ioctl', ioctl)


def test_contiguous_written_mapping(monkeypatch):
    response(monkeypatch)
    assert ext._mapping(9, 8192, time.monotonic() + 10) == ((0, 4096, 8192, 1),)


@pytest.mark.parametrize('flags', [0, 2, 4, 8, 0x80, 0x100, 0x200, 0x400,
                                    0x800, 0x1000, 0x2000, 0x80000000])
def test_missing_last_or_unapproved_flags(monkeypatch, flags):
    response(monkeypatch, flags=flags)
    with pytest.raises(ValueError):
        ext._mapping(9, 8192, time.monotonic() + 10)


@pytest.mark.parametrize('changes', [dict(logical=4096), dict(physical=0),
                                    dict(physical=1), dict(length=4096),
                                    dict(length=12288), dict(count=0),
                                    dict(count=257), dict(reserved=1)])
def test_holes_alignment_bounds_and_protocol(monkeypatch, changes):
    response(monkeypatch, **changes)
    with pytest.raises(ValueError):
        ext._mapping(9, 8192, time.monotonic() + 10)


def test_expired_before_ioctl(monkeypatch):
    def unexpected(*args):
        pytest.fail('ioctl after expired deadline')
    monkeypatch.setattr(ext.fcntl, 'ioctl', unexpected)
    with pytest.raises(ValueError, match='RESOURCE_LIMIT'):
        ext._mapping(9, 8192, time.monotonic() - 1)
