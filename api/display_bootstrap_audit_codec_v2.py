"""Fixed audit V2 envelope codec; V1 records remain read-only elsewhere."""
import hashlib
from api import display_bootstrap_audit_codec as legacy
from api.display_bootstrap_manifest import canonical_bytes, parse_record
from api.display_bootstrap_v3 import validate_actor, validate_mapping

HEADER_BYTES = legacy.HEADER_BYTES
CONTROL_BYTES = legacy.CONTROL_BYTES
PAYLOAD_BYTES = legacy.PAYLOAD_BYTES
SLOT_BYTES = legacy.SLOT_BYTES
file_bytes = legacy.file_bytes


def encode_header(value):
    if type(value) is not dict or type(value.get('format_version')) is not int or value['format_version'] != 2:
        raise ValueError('INVALID_INPUT')
    legacy.encode_header(dict(value, format_version=1))
    raw = canonical_bytes(value)
    return legacy._seal(legacy._HEADER.pack(b'HBAUD002', len(raw)) + raw)


def decode_header(raw):
    body = legacy._unseal(raw)
    magic, length = legacy._HEADER.unpack_from(body)
    if magic != b'HBAUD002' or not 1 <= length <= 4052:
        raise ValueError('AUDIT_UNAVAILABLE')
    value = parse_record(body[12:12 + length])
    if encode_header(value) != raw:
        raise ValueError('AUDIT_UNAVAILABLE')
    return value


def validate_payload(kind, raw, candidate_id, slots):
    envelope = parse_record(raw)
    if (set(envelope) != {'format_version', 'actor', 'record'}
            or type(envelope['format_version']) is not int or envelope['format_version'] != 2):
        raise ValueError('INVALID_INPUT')
    if (type(kind) is not int or kind not in (1, 2, 3)
            or type(envelope['record']) is not dict):
        raise ValueError('INVALID_INPUT')
    actor = validate_actor(envelope['actor'])
    record_raw = canonical_bytes(envelope['record'])
    if len(record_raw) > 60000:
        raise ValueError('INVALID_INPUT')
    if kind == 1 and envelope['record'].get('format_version') == 2:
        from api.display_bootstrap_v4 import validate_registry
        legacy.file_bytes(slots)
        record = validate_registry(envelope['record'])
        if record['candidate_id'] != candidate_id or record['seq'] > slots:
            raise ValueError('INVALID_INPUT')
        key = hashlib.sha256(('registry:%020d' % record['seq']).encode('ascii')).digest()
    else:
        record, key = legacy.validate_payload(kind, record_raw, candidate_id, slots)
    role = actor['role']
    if kind == 2 and role != 'creator':
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    if kind == 1:
        if record['state'] in ('RESERVED', 'BUILDING', 'VERIFIED') and role != 'creator':
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        if record['state'] in ('APPROVED', 'PUBLISH_INTENT', 'PUBLISHED_UNACTIVATED') and role not in ('publisher', 'recover'):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    if kind == 3 and record['operation'] != {'creator': 'create', 'publisher': 'publish', 'recover': 'recover'}[role]:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    if kind != 1:
        key = hashlib.sha256(raw).digest()
    return envelope, key


def bind_actor(envelope, resource):
    if envelope['record'].get('state') is not None:
        expected = 2 if resource['format_version'] == 4 else 1
        if envelope['record'].get('format_version') != expected:
            raise ValueError('APPROVAL_MISMATCH')
    mapping = validate_mapping(resource['hard_limit_profiles'])
    actor = validate_actor(envelope['actor'])
    if mapping[actor['role']] != {k: actor[k] for k in ('profile_id', 'profile_sha')}:
        raise ValueError('APPROVAL_MISMATCH')


def encode_slot(header, header_sha, index, kind, payload):
    if encode_header(header)[4064:] != legacy._hex(header_sha, 64):
        raise ValueError('INVALID_INPUT')
    if type(index) is not int or not 0 <= index < header['slot_count'] or not 1 <= len(payload) <= PAYLOAD_BYTES:
        raise ValueError('INVALID_INPUT')
    _, key = validate_payload(kind, payload, header['candidate_id'], header['slot_count'])
    fields = (2, index, kind, len(payload), legacy._hex(header['candidate_id'], 32), key,
              hashlib.sha256(payload).digest(), legacy._hex(header_sha, 64))
    return (legacy._seal(legacy._PREFIX.pack(b'HBCLM002', *fields))
            + payload.ljust(PAYLOAD_BYTES, b'\0')
            + legacy._seal(legacy._PREFIX.pack(b'HBCMT002', *fields)))


def decode_slot(header, header_sha, index, raw):
    if type(raw) is not bytes or len(raw) != SLOT_BYTES:
        raise ValueError('AUDIT_UNAVAILABLE')
    if (type(index) is not int or not 0 <= index < header['slot_count']
            or encode_header(header)[4064:] != legacy._hex(header_sha, 64)):
        raise ValueError('INVALID_INPUT')
    if raw == bytes(SLOT_BYTES):
        return None
    body = legacy._unseal(raw[:CONTROL_BYTES])
    magic, version, physical, kind, length, *_ = legacy._PREFIX.unpack_from(body)
    if magic != b'HBCLM002' or version != 2 or physical != index or not 1 <= length <= PAYLOAD_BYTES:
        raise ValueError('AUDIT_UNAVAILABLE')
    payload = raw[CONTROL_BYTES:CONTROL_BYTES + length]
    if encode_slot(header, header_sha, index, kind, payload) != raw:
        raise ValueError('AUDIT_UNAVAILABLE')
    envelope, key = validate_payload(kind, payload, header['candidate_id'], header['slot_count'])
    return dict(kind=kind, logical_key=key.hex(), payload=payload, envelope=envelope,
                value=envelope['record'], physical_slot=index,
                logical_sha=hashlib.sha256(payload).hexdigest())
