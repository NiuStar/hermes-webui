"""Bounded-growth metadata; never upgrades supplied historical records."""
from api.display_bootstrap_v3 import hex_value

MAX_INTEGER = 9007199254740991
GROWTH_FIELDS = frozenset('format_version authority_id authority_sha total_bytes total_inodes '
    'host_min_free_bytes host_min_free_inodes audit_min_free_bytes audit_min_free_inodes '
    'candidate_peak_inodes candidate_metadata_bytes candidate_metadata_inodes '
    'fixed_bytes fixed_inodes observation_max_age_ms'.split())


def checked(value):
    if type(value) is not int or not 0 <= value <= MAX_INTEGER:
        raise ValueError('INVALID_INPUT')
    return value


def round_block(value):
    checked(value)
    return checked(checked(value + 4095) // 4096 * 4096)


def validate_growth(value):
    if type(value) is not dict or set(value) != GROWTH_FIELDS:
        raise ValueError('INVALID_INPUT')
    hex_value(value['authority_id'], 32)
    hex_value(value['authority_sha'], 64)
    for key in GROWTH_FIELDS - {'authority_id', 'authority_sha'}:
        if checked(value[key]) < 1:
            raise ValueError('INVALID_INPUT')
    for key, expected in {'format_version': 1, 'candidate_metadata_bytes': 262144,
                          'candidate_metadata_inodes': 8,
                          'observation_max_age_ms': 1000}.items():
        if value[key] != expected:
            raise ValueError('INVALID_INPUT')
    if value['candidate_peak_inodes'] < 8:
        raise ValueError('INVALID_INPUT')
    return value


def validate_resource(value):
    from api.display_bootstrap_manifest import validate_record
    shared = dict(value)
    growth = validate_growth(shared.pop('growth', None))
    shared['format_version'] = 3
    validate_record(shared, 'resource')
    if value['audit_slot_count'] != 16 or value['audit_reserve_bytes'] != 1183744:
        raise ValueError('INVALID_INPUT')
    # Check the largest permitted commitment, not only today's count.
    count = value['max_retained_candidates']
    peak = checked(round_block(value['max_candidate_bytes']) +
                   value['audit_reserve_bytes'] + growth['candidate_metadata_bytes'])
    inodes = checked(growth['candidate_peak_inodes'] + 2 + growth['candidate_metadata_inodes'])
    checked(growth['fixed_bytes'] + checked(count * peak))
    checked(growth['fixed_inodes'] + checked(count * inodes))
    return value


def validate_registry(value):
    from api.display_bootstrap_manifest import validate_record
    if type(value) is not dict or type(value.get('format_version')) is not int or value['format_version'] != 2:
        raise ValueError('INVALID_INPUT')
    shared = dict(value)
    hex_value(shared.pop('request_sha', None), 64)
    if type(shared.get('seq')) is not int or not 1 <= shared['seq'] <= 16:
        raise ValueError('INVALID_INPUT')
    shared['format_version'] = 1
    validate_record(shared, 'registry')
    return value
