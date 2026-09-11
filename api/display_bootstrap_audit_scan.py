"""Validate a complete fixed log before exposing any business records.

The caller owns the lock, file identity checks, deadline and exact bounded
pread callback. This module neither opens files nor grants write authority.
"""
from api.display_bootstrap_audit_codec import (
    HEADER_BYTES, SLOT_BYTES, decode_header, decode_slot,
)

_TRANSITIONS = {
    'RESERVED': ('BUILDING', 'FAILED', 'QUARANTINED'),
    'BUILDING': ('VERIFIED', 'FAILED', 'QUARANTINED'),
    'VERIFIED': ('APPROVED', 'FAILED', 'QUARANTINED'),
    'APPROVED': ('PUBLISH_INTENT', 'FAILED', 'QUARANTINED'),
    'PUBLISH_INTENT': ('PUBLISHED_UNACTIVATED', 'QUARANTINED'),
    'PUBLISHED_UNACTIVATED': (), 'FAILED': (), 'QUARANTINED': (),
}


def scan(read_exact, *, codec=None, resource=None):
    """Return bounded receipts only after *all* physical slots validate.

    read_exact(offset, length) must reject short reads and check the deadline.
    Payload bytes are not retained: memory scales with slot metadata, not file
    size. A reader must revalidate identity and reread selected payloads later.
    """
    if codec is None:
        from api import display_bootstrap_audit_codec as codec
    header_raw = read_exact(0, HEADER_BYTES)
    header = codec.decode_header(header_raw)
    if header['format_version'] == 2:
        from api.display_bootstrap_manifest import canonical_bytes, digest, validate_record
        validate_record(resource, 'resource')
        if resource['format_version'] not in (3, 4) or digest(canonical_bytes(resource)) != header['resource_sha']:
            raise ValueError('APPROVAL_MISMATCH')
    header_sha = header_raw[4064:].hex()
    receipts, keys = [], set()
    zero_tail = False
    previous = None
    previous_sha = None
    platform_sha = None
    failure = None
    for index in range(header['slot_count']):
        raw = read_exact(HEADER_BYTES + index * SLOT_BYTES, SLOT_BYTES)
        item = codec.decode_slot(header, header_sha, index, raw)
        if item is not None and header['format_version'] == 2:
            codec.bind_actor(item['envelope'], resource)
        if item is None:
            zero_tail = True
            continue
        if zero_tail or (previous is not None and not _TRANSITIONS[previous['state']]):
            raise ValueError('AUDIT_UNAVAILABLE')
        key = item['kind'], item['logical_key']
        if key in keys:
            raise ValueError('AUDIT_UNAVAILABLE')
        keys.add(key)
        value = item['value']
        if item['kind'] == 1:
            if previous is None:
                if index != 0 or value['state'] != 'RESERVED' or value['seq'] != 1:
                    raise ValueError('AUDIT_UNAVAILABLE')
            else:
                if (value['state'] not in _TRANSITIONS[previous['state']]
                        or value['seq'] != previous['seq'] + 1):
                    raise ValueError('AUDIT_UNAVAILABLE')
                for field in ('manifest_sha', 'approval_id'):
                    if previous[field] is not None and value[field] != previous[field]:
                        raise ValueError('AUDIT_UNAVAILABLE')
            if previous is not None and (value['format_version'] != previous['format_version']
                    or value.get('request_sha') != previous.get('request_sha')):
                raise ValueError('AUDIT_UNAVAILABLE')
            if value['previous_sha'] != previous_sha:
                raise ValueError('AUDIT_UNAVAILABLE')
            if value['state'] == 'VERIFIED' and platform_sha is None:
                raise ValueError('AUDIT_UNAVAILABLE')
            if value['state'] in ('FAILED', 'QUARANTINED'):
                if (failure is None or failure['error_code'] != value['error_code']
                        or failure['registry_seq'] != previous['seq']):
                    raise ValueError('AUDIT_UNAVAILABLE')
            elif failure is not None:
                raise ValueError('AUDIT_UNAVAILABLE')
            previous, previous_sha = value, item['logical_sha']
        elif item['kind'] == 2:
            if (previous is None or previous['state'] != 'BUILDING'
                    or platform_sha is not None or failure is not None):
                raise ValueError('AUDIT_UNAVAILABLE')
            platform_sha = item['logical_sha']
        else:
            if (previous is None or failure is not None
                    or value['registry_seq'] != previous['seq']):
                raise ValueError('AUDIT_UNAVAILABLE')
            failure = value
        receipts.append({k: item[k] for k in
                         ('kind', 'logical_key', 'physical_slot', 'logical_sha')})
    return dict(header=header, header_sha=header_sha, receipts=receipts,
                remaining_slots=header['slot_count'] - len(receipts),
                registry_last=previous, registry_sha=previous_sha,
                platform_sha=platform_sha,
                pending_failure=failure if previous and previous['state'] not in
                ('FAILED', 'QUARANTINED') else None)
