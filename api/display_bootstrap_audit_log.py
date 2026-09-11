"""Fixed audit persistence primitives; not Context or Landlock authority.

Caller holds the global lock and supplies a deadline plus live allocation
verification. No defaults bypass those requirements. Production wiring remains
closed until role confinement and storage qualification are implemented.
"""
import math
import os
import stat
import time

from api.display_bootstrap_audit_codec import (
    HEADER_BYTES, CONTROL_BYTES, PAYLOAD_BYTES, SLOT_BYTES,
    decode_header, decode_slot, encode_header, encode_slot, file_bytes,
)
from api.display_bootstrap_audit_scan import scan


_FIELDS = ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode', 'st_nlink',
           'st_size', 'st_blocks')


def _deadline(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('INVALID_INPUT')
    if time.monotonic() >= value:
        raise ValueError('RESOURCE_LIMIT')


def prepare(registry_fd, candidate_id, deployment_sha, resource_sha, slots,
            reserved_payload, *, deadline, verify_allocation, resource=None):
    """Prepare one new container; retain every partial artifact on failure.

    Caller must already hold the global lock and pass live admission checks.
    Only RESERVED may be written before confinement. Return metadata, never a
    writable fd. This primitive does not issue a BootstrapContext.
    """
    from api import display_bootstrap_audit_codec as codec
    version = 1
    if resource is not None:
        from api import display_bootstrap_audit_codec_v2 as codec
        from api.display_bootstrap_manifest import validate_record, digest, canonical_bytes
        validate_record(resource, 'resource')
        if resource['format_version'] not in (3, 4) or digest(canonical_bytes(resource)) != resource_sha:
            raise ValueError('APPROVAL_MISMATCH')
        version = 2
    validate_payload = codec.validate_payload
    encode_header = codec.encode_header

    _deadline(deadline)
    if not callable(verify_allocation):
        raise ValueError('INVALID_INPUT')
    size = file_bytes(slots)
    record, _ = validate_payload(1, reserved_payload, candidate_id, slots)
    if version == 2:
        codec.bind_actor(record, resource)
        record = record['record']
    if record['state'] != 'RESERVED' or record['seq'] != 1:
        raise ValueError('INVALID_INPUT')
    # Validate all header inputs before creating any persistent name.
    template = dict(format_version=version, candidate_id=candidate_id,
                    file_identity=dict(dev=1, ino=1, uid=os.geteuid(),
                                       gid=os.getegid(), mode=stat.S_IFREG | 0o600, nlink=1),
                    deployment_sha=deployment_sha, resource_sha=resource_sha,
                    slot_count=slots, slot_bytes=SLOT_BYTES, payload_bytes=PAYLOAD_BYTES)
    encode_header(template)
    directory = fd = None
    try:
        parent = os.fstat(registry_fd)
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
                or parent.st_mode & 0o022
                or any(k.startswith('system.posix_acl_') for k in os.listxattr(registry_fd))):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        os.mkdir(candidate_id, 0o700, dir_fd=registry_fd)
        directory = os.open(candidate_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                            | os.O_CLOEXEC, dir_fd=registry_fd)
        directory_identity = os.fstat(directory)
        if (directory_identity.st_dev != parent.st_dev
                or stat.S_IMODE(directory_identity.st_mode) != 0o700):
            raise ValueError('IDENTITY_CHANGED')
        os.fsync(registry_fd)
        fd = os.open('audit.bin', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                     | os.O_CLOEXEC, 0o600, dir_fd=directory)
        os.posix_fallocate(fd, 0, size)

        def write_exact(offset, data):
            if offset < 0 or offset + len(data) > size:
                raise ValueError('INVALID_INPUT')
            cursor = 0
            while cursor < len(data):
                _deadline(deadline)
                count = os.pwrite(fd, data[cursor:], offset + cursor)
                if not 0 < count <= len(data) - cursor:
                    raise OSError('short audit initialization write')
                cursor += count
            _deadline(deadline)

        zero = bytes(PAYLOAD_BYTES)
        for offset in range(0, size, PAYLOAD_BYTES):
            write_exact(offset, zero[:min(PAYLOAD_BYTES, size - offset)])
        os.fsync(fd)
        os.fsync(directory)
        verify_allocation(fd, size, deadline)
        template['file_identity'] = _identity(os.fstat(fd))
        raw = encode_header(template)
        write_exact(0, raw[:4064])
        os.fsync(fd)
        write_exact(4064, raw[4064:])
        os.fsync(fd)
        closing_fd, fd = fd, None
        os.close(closing_fd)
        # Internal, single-purpose pre-confinement writer. Never exported.
        with AuditFile(directory, template, deadline=deadline,
                       verify_allocation=verify_allocation, writable=True, resource=resource) as log:
            receipt = log.append(1, reserved_payload)
            log.confirm_durable()
        named = os.stat(candidate_id, dir_fd=registry_fd, follow_symlinks=False)
        after = os.fstat(directory)
        for other in (named, after):
            if _identity(other) != _identity(directory_identity):
                raise ValueError('IDENTITY_CHANGED')
        _deadline(deadline)
        return dict(header=template, header_sha=raw[4064:].hex(), reserved_receipt=receipt)
    finally:
        try:
            if fd is not None:
                os.close(fd)
        finally:
            if directory is not None:
                os.close(directory)


def _identity(info):
    return {key: getattr(info, 'st_' + key)
            for key in ('dev', 'ino', 'uid', 'gid', 'mode', 'nlink')}


class AuditFile:
    """Private fd, bounded I/O, sticky failures; never an admission token."""

    def __init__(self, directory_fd, expected_header, *, deadline,
                 verify_allocation, writable=False, resource=None):
        _deadline(deadline)
        if not callable(verify_allocation) or type(writable) is not bool:
            raise ValueError('INVALID_INPUT')
        from api import display_bootstrap_audit_codec as codec
        if expected_header.get('format_version') == 2:
            from api import display_bootstrap_audit_codec_v2 as codec
        self._codec = codec
        import copy
        self._resource = copy.deepcopy(resource)
        self._header_raw = codec.encode_header(expected_header)
        self._header = codec.decode_header(self._header_raw)
        self._size = file_bytes(self._header['slot_count'])
        self._deadline = deadline
        self._verify_allocation = verify_allocation
        self._pid = os.getpid()
        self._writable = writable
        self._broken = False
        self._fd = self._directory = None
        try:
            self._directory = os.dup(directory_fd)
            self._fd = os.open('audit.bin', (os.O_RDWR if writable else os.O_RDONLY)
                               | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                               dir_fd=self._directory)
            info = self._check()
            self._baseline = info
            if self._read(0, HEADER_BYTES) != self._header_raw:
                raise ValueError('IDENTITY_CHANGED')
            self.inspect()
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        # Cleanup must work after the deadline, partial construction or fork.
        try:
            if self._fd is not None:
                fd, self._fd = self._fd, None
                os.close(fd)
        finally:
            if self._directory is not None:
                fd, self._directory = self._directory, None
                os.close(fd)

    def _check(self):
        if self._pid != os.getpid() or self._fd is None or self._broken:
            raise ValueError('AUDIT_UNAVAILABLE')
        _deadline(self._deadline)
        info = os.fstat(self._fd)
        named = os.stat('audit.bin', dir_fd=self._directory, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or _identity(info) != self._header['file_identity']
                or info.st_size != self._size or info.st_blocks * 512 < self._size
                or any(getattr(info, k) != getattr(named, k) for k in _FIELDS)
                or any(k.startswith('system.posix_acl_') for k in os.listxattr(self._fd))):
            raise ValueError('IDENTITY_CHANGED')
        if hasattr(self, '_baseline') and any(
                getattr(info, k) != getattr(self._baseline, k) for k in _FIELDS):
            raise ValueError('IDENTITY_CHANGED')
        # Only the single fixed file is allowed; no silent legacy mixing.
        with os.scandir(self._directory) as entries:
            first = next(entries, None)
            if first is None or first.name != 'audit.bin' or next(entries, None) is not None:
                raise ValueError('AUDIT_UNAVAILABLE')
        return info

    def _range(self, offset, length):
        if (type(offset) is not int or type(length) is not int
                or offset < 0 or length < 1 or length > SLOT_BYTES
                or offset > self._size - length):
            raise ValueError('INVALID_INPUT')

    def _read(self, offset, length):
        self._range(offset, length)
        parts = []
        remaining = length
        while remaining:
            _deadline(self._deadline)
            part = os.pread(self._fd, remaining, offset)
            if not part:
                raise ValueError('AUDIT_UNAVAILABLE')
            parts.append(part)
            offset += len(part)
            remaining -= len(part)
        _deadline(self._deadline)
        return b''.join(parts)

    def inspect(self):
        try:
            before = self._check()
            self._verify_allocation(self._fd, self._size, self._deadline)
            result = scan(self._read, codec=self._codec, resource=self._resource)
            after = self._check()
            if (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('IDENTITY_CHANGED')
            if result['header'] != self._header:
                raise ValueError('IDENTITY_CHANGED')
            return result
        except BaseException:
            self._broken = True
            raise

    def confirm_durable(self):
        try:
            self._check()
            os.fsync(self._fd)
            return self.inspect()
        except BaseException:
            self._broken = True
            raise

    def records(self, kind):
        """Read one kind in two full scans, not one full scan per record."""
        try:
            if type(kind) is not int or kind not in (1, 2, 3):
                raise ValueError('INVALID_INPUT')
            before = self.inspect()
            result = []
            total = 0
            for receipt in before['receipts']:
                if receipt['kind'] != kind:
                    continue
                index = receipt['physical_slot']
                item = self._codec.decode_slot(self._header, before['header_sha'], index,
                    self._read(HEADER_BYTES + index * SLOT_BYTES, SLOT_BYTES))
                if item is None or any(item[k] != receipt[k] for k in receipt):
                    raise ValueError('IDENTITY_CHANGED')
                total += len(item['payload'])
                # Registry records are small; never materialize the full 300MB
                # container even for hostile diagnostic payloads.
                if total > 4 * 1024 * 1024:
                    raise ValueError('RESOURCE_LIMIT')
                result.append(item['value'])
            if self.inspect() != before:
                raise ValueError('IDENTITY_CHANGED')
            return result
        except BaseException:
            self._broken = True
            raise

    def get(self, kind, logical_key):
        try:
            result = self.inspect()
            selected = [r for r in result['receipts']
                        if (r['kind'], r['logical_key']) == (kind, logical_key)]
            if not selected:
                return None
            receipt = selected[0]
            index = receipt['physical_slot']
            item = self._codec.decode_slot(self._header, result['header_sha'], index,
                               self._read(HEADER_BYTES + index * SLOT_BYTES, SLOT_BYTES))
            if item is None or any(item[k] != receipt[k] for k in receipt):
                raise ValueError('IDENTITY_CHANGED')
            if self.inspect() != result:
                raise ValueError('IDENTITY_CHANGED')
            return item['payload']
        except BaseException:
            self._broken = True
            raise

    def _write_sync(self, offset, data):
        self._range(offset, len(data))
        if not self._writable:
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        self._check()
        cursor = 0
        while cursor < len(data):
            _deadline(self._deadline)
            count = os.pwrite(self._fd, data[cursor:], offset + cursor)
            if not 0 < count <= len(data) - cursor:
                raise OSError('short audit write')
            cursor += count
        self._check()
        os.fsync(self._fd)
        self._check()
        if self._read(offset, len(data)) != data:
            raise ValueError('AUDIT_UNAVAILABLE')

    def append(self, kind, payload):
        try:
            if not self._writable:
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
            current = self.inspect()
            if not current['remaining_slots']:
                raise ValueError('AUDIT_UNAVAILABLE')
            index = len(current['receipts'])
            offset = HEADER_BYTES + index * SLOT_BYTES
            raw = self._codec.encode_slot(self._header, current['header_sha'], index, kind, payload)
            # Validate the prospective *whole* logical chain before any write.
            def prospective(position, length):
                return raw if position == offset and length == SLOT_BYTES else self._read(position, length)
            expected = scan(prospective, codec=self._codec, resource=self._resource)
            if self.inspect() != current:
                raise ValueError('IDENTITY_CHANGED')
            self._write_sync(offset, raw[:CONTROL_BYTES])
            self._write_sync(offset + CONTROL_BYTES,
                             raw[CONTROL_BYTES:CONTROL_BYTES + PAYLOAD_BYTES])
            self._write_sync(offset + CONTROL_BYTES + PAYLOAD_BYTES, raw[-CONTROL_BYTES:])
            actual = self.inspect()
            if actual != expected:
                raise ValueError('AUDIT_UNAVAILABLE')
            return actual['receipts'][-1]
        except BaseException:
            self._broken = True
            raise
