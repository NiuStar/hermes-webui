"""Persistence checks run only on an explicitly supplied isolated test root.

Allocation callback here checks blocks only: these tests do NOT qualify storage.
Files, including damaged containers, are deliberately retained.
"""
import os
from pathlib import Path
import stat
import tempfile
import time

import pytest

from api.display_bootstrap_audit_codec import encode_header, file_bytes
from api.display_bootstrap_audit_log import AuditFile
from api.display_bootstrap_manifest import canonical_bytes


@pytest.fixture
def log_file(request):
    root = os.environ.get('LIFECYCLE_AUDIT_TEST_ROOT')
    if not root or not Path(root).is_dir():
        pytest.fail('existing isolated LIFECYCLE_AUDIT_TEST_ROOT required')
    directory = tempfile.mkdtemp(prefix='fixed-log-', dir=root)
    parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    request.addfinalizer(lambda: os.close(parent))
    fd = os.open('audit.bin', os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600, dir_fd=parent)
    try:
        info = os.fstat(fd)
        header = dict(format_version=1, candidate_id='a' * 32,
                      file_identity={k: getattr(info, 'st_' + k) for k in
                                     ('dev', 'ino', 'uid', 'gid', 'mode', 'nlink')},
                      deployment_sha='b' * 64, resource_sha='c' * 64,
                      slot_count=9, slot_bytes=73728, payload_bytes=65536)
        raw = encode_header(header) + bytes(file_bytes(9) - 4096)
        cursor = 0
        while cursor < len(raw):
            count = os.write(fd, raw[cursor:])
            if count <= 0:
                raise OSError('short fixture write')
            cursor += count
        os.fsync(fd)
        os.fsync(parent)
    finally:
        os.close(fd)
    return parent, header


def allocation(fd, size, deadline):
    assert time.monotonic() < deadline
    assert os.fstat(fd).st_blocks * 512 >= size


def open_log(pair, writable=True):
    return AuditFile(*pair, deadline=time.monotonic() + 60,
                     verify_allocation=allocation, writable=writable)


def reserved():
    return canonical_bytes(dict(format_version=1, candidate_id='a' * 32, seq=1,
                                previous_sha=None, state='RESERVED', target_name='a' * 32,
                                manifest_sha=None, approval_id=None, error_code=None))


def test_prepare_commits_reserved_without_reusing_id(log_file):
    from api.display_bootstrap_audit_log import prepare
    parent, _ = log_file
    result = prepare(parent, 'a' * 32, 'b' * 64, 'c' * 64, 9, reserved(),
                     deadline=time.monotonic() + 60, verify_allocation=allocation)
    child = os.open('a' * 32, os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent)
    try:
        with open_log((child, result['header']), False) as log:
            assert log.inspect()['registry_last']['state'] == 'RESERVED'
            assert log.inspect()['remaining_slots'] == 8
        with pytest.raises(FileExistsError):
            prepare(parent, 'a' * 32, 'b' * 64, 'c' * 64, 9, reserved(),
                    deadline=time.monotonic() + 60, verify_allocation=allocation)
    finally:
        os.close(child)


def test_prepare_allocation_failure_retains_container(log_file):
    from api.display_bootstrap_audit_log import prepare
    parent, _ = log_file
    def reject(*args):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    with pytest.raises(ValueError, match='ACCESS_BOUNDARY_UNPROVEN'):
        prepare(parent, 'a' * 32, 'b' * 64, 'c' * 64, 9, reserved(),
                deadline=time.monotonic() + 60, verify_allocation=reject)
    child = os.open('a' * 32, os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent)
    try:
        assert os.stat('audit.bin', dir_fd=child).st_size == file_bytes(9)
    finally:
        os.close(child)


def test_append_readback(log_file):
    with open_log(log_file) as log:
        receipt = log.append(1, reserved())
        assert log.get(1, receipt['logical_key']) == reserved()
        assert log.confirm_durable()['remaining_slots'] == 8
    with open_log(log_file, False) as log:
        assert log.inspect()['registry_last']['state'] == 'RESERVED'


def test_short_writes_complete(log_file, monkeypatch):
    real = os.pwrite
    monkeypatch.setattr(os, 'pwrite', lambda fd, data, offset: real(fd, data[:137], offset))
    with open_log(log_file) as log:
        assert log.append(1, reserved())['physical_slot'] == 0


def test_sync_failure_poisoned_and_preserved(log_file, monkeypatch):
    with open_log(log_file) as log:
        def fail(fd):
            raise OSError('injected fsync failure')
        with monkeypatch.context() as m:
            m.setattr(os, 'fsync', fail)
            with pytest.raises(OSError):
                log.append(1, reserved())
        with pytest.raises(ValueError, match='AUDIT_UNAVAILABLE'):
            log.inspect()
    with pytest.raises(ValueError):
        open_log(log_file)
    assert stat.S_ISREG(os.stat('audit.bin', dir_fd=log_file[0]).st_mode)


def test_invalid_transition_no_write(log_file):
    with open_log(log_file) as log:
        log.append(1, reserved())
        before = os.stat('audit.bin', dir_fd=log_file[0]).st_mtime_ns
        with pytest.raises(ValueError):
            log.append(1, reserved())
        assert os.stat('audit.bin', dir_fd=log_file[0]).st_mtime_ns == before


def test_readonly_cannot_append(log_file):
    with open_log(log_file, False) as log:
        with pytest.raises(ValueError, match='ACCESS_BOUNDARY_UNPROVEN'):
            log.append(1, reserved())
