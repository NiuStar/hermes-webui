"""Fixed audit V1 bytes. Pure codec; grants no filesystem/admission authority."""
import hashlib
import re
import struct

from api.display_bootstrap_manifest import canonical_bytes, parse_record, validate_record

HEADER_BYTES = 4096
CONTROL_BYTES = 4096
PAYLOAD_BYTES = 65536
SLOT_BYTES = CONTROL_BYTES * 2 + PAYLOAD_BYTES
_PREFIX = struct.Struct('<8sIIII16s32s32s32s')
_HEADER = struct.Struct('<8sI')


def _hex(value, length):
    if type(value) is not str or re.fullmatch('[0-9a-f]{%d}' % length, value) is None:
        raise ValueError('INVALID_INPUT')
    return bytes.fromhex(value)


def file_bytes(slots):
    if type(slots) is not int or not 9 <= slots <= 4096:
        raise ValueError('INVALID_INPUT')
    return HEADER_BYTES + slots * SLOT_BYTES


def _seal(raw):
    if len(raw) > 4064:
        raise ValueError('INVALID_INPUT')
    body = raw.ljust(4064, b'\0')
    return body + hashlib.sha256(body).digest()


def _unseal(raw):
    if (type(raw) is not bytes or len(raw) != 4096
            or hashlib.sha256(raw[:4064]).digest() != raw[4064:]):
        raise ValueError('AUDIT_UNAVAILABLE')
    return raw[:4064]


def encode_header(value):
    expected = {'format_version', 'candidate_id', 'file_identity', 'deployment_sha',
                'resource_sha', 'slot_count', 'slot_bytes', 'payload_bytes'}
    if type(value) is not dict or set(value) != expected:
        raise ValueError('INVALID_INPUT')
    for key, expected_value in [('format_version', 1), ('slot_bytes', SLOT_BYTES),
                                ('payload_bytes', PAYLOAD_BYTES)]:
        if type(value[key]) is not int or value[key] != expected_value:
            raise ValueError('INVALID_INPUT')
    file_bytes(value['slot_count'])
    _hex(value['candidate_id'], 32)
    _hex(value['deployment_sha'], 64)
    _hex(value['resource_sha'], 64)
    validate_record(value['file_identity'], 'file_identity')
    raw = canonical_bytes(value)
    return _seal(_HEADER.pack(b'HBAUD001', len(raw)) + raw)


def decode_header(raw):
    body = _unseal(raw)
    magic, length = _HEADER.unpack_from(body)
    if magic != b'HBAUD001' or not 1 <= length <= 4052:
        raise ValueError('AUDIT_UNAVAILABLE')
    value = parse_record(body[12:12 + length])
    if encode_header(value) != raw:
        raise ValueError('AUDIT_UNAVAILABLE')
    return value


def validate_payload(kind, raw, candidate_id, slots):
    file_bytes(slots)
    if type(kind) is not int or kind not in (1, 2, 3):
        raise ValueError('INVALID_INPUT')
    _hex(candidate_id, 32)
    value = parse_record(raw)
    if kind == 1:
        validate_record(value, 'registry')
        if value['format_version'] != 1:
            raise ValueError('INVALID_INPUT')
        if value['candidate_id'] != candidate_id or value['seq'] > slots:
            raise ValueError('INVALID_INPUT')
        key = hashlib.sha256(('registry:%020d' % value['seq']).encode('ascii')).digest()
    elif kind == 2:
        validate_record(value, 'platform_evidence')
        key = hashlib.sha256(raw).digest()
    else:
        fields = {'format_version', 'candidate_id', 'error_code', 'operation',
                  'stage', 'registry_seq', 'detail'}
        if (set(value) != fields or type(value['format_version']) is not int
                or value['format_version'] != 1 or value['candidate_id'] != candidate_id
                or value['operation'] not in ('create', 'publish', 'recover')
                or value['stage'] not in ('PREPARE', 'BUILD', 'VERIFY', 'APPROVAL',
                                          'INTENT', 'MOVE', 'COMPLETE', 'RECOVER')
                or type(value['registry_seq']) is not int
                or not 1 <= value['registry_seq'] <= slots
                or type(value['detail']) is not str or not value['detail']
                or len(value['detail'].encode('utf-8')) > 4096):
            raise ValueError('INVALID_INPUT')
        # Reuse the authoritative error enum via a valid failure record.
        validate_record(dict(format_version=1, candidate_id=candidate_id, seq=1,
                             previous_sha=None, state='FAILED', target_name=candidate_id,
                             manifest_sha=None, approval_id=None,
                             error_code=value['error_code']), 'registry')
        key = hashlib.sha256(raw).digest()
    return value, key


def encode_slot(header, header_sha, index, kind, payload):
    if encode_header(header)[4064:] != _hex(header_sha, 64):
        raise ValueError('INVALID_INPUT')
    if type(index) is not int or not 0 <= index < header['slot_count']:
        raise ValueError('INVALID_INPUT')
    _, key = validate_payload(kind, payload, header['candidate_id'], header['slot_count'])
    fields = (1, index, kind, len(payload), _hex(header['candidate_id'], 32), key,
              hashlib.sha256(payload).digest(), _hex(header_sha, 64))
    claim = _seal(_PREFIX.pack(b'HBCLM001', *fields))
    commit = _seal(_PREFIX.pack(b'HBCMT001', *fields))
    return claim + payload.ljust(PAYLOAD_BYTES, b'\0') + commit


def decode_slot(header, header_sha, index, raw):
    if type(raw) is not bytes or len(raw) != SLOT_BYTES:
        raise ValueError('AUDIT_UNAVAILABLE')
    if (type(index) is not int or not 0 <= index < header['slot_count']
            or encode_header(header)[4064:] != _hex(header_sha, 64)):
        raise ValueError('INVALID_INPUT')
    if raw == bytes(SLOT_BYTES):
        return None
    claim = _unseal(raw[:CONTROL_BYTES])
    _unseal(raw[-CONTROL_BYTES:])
    magic, version, physical, kind, length, *_ = _PREFIX.unpack_from(claim)
    if (magic != b'HBCLM001' or version != 1 or physical != index
            or not 1 <= length <= PAYLOAD_BYTES):
        raise ValueError('AUDIT_UNAVAILABLE')
    payload = raw[CONTROL_BYTES:CONTROL_BYTES + length]
    if encode_slot(header, header_sha, index, kind, payload) != raw:
        raise ValueError('AUDIT_UNAVAILABLE')
    value, key = validate_payload(kind, payload, header['candidate_id'], header['slot_count'])
    return dict(kind=kind, logical_key=key.hex(), payload=payload, value=value,
                physical_slot=index, logical_sha=hashlib.sha256(payload).hexdigest())
