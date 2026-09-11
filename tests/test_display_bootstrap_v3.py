"""V3 schema regression cases, not operating-system admission tests."""
import copy
import pytest
from api.display_bootstrap_manifest import validate_record
from api.display_bootstrap_v3 import validate_mapping


def resource():
    return dict(format_version=3, max_candidate_bytes=4096, min_free_bytes=4096,
        max_retained_candidates=1, max_rss_bytes=67108864, max_elapsed_seconds=60,
        check_interval_ms=100, audit_reserve_bytes=4096 + 9 * (8192 + 65536), audit_slot_count=9,
        audit_format='FIXED_LOG_V2', hard_limit_profiles={
            role: dict(profile_id=str(i)*32, profile_sha=str(i)*64)
            for i, role in enumerate(('creator', 'publisher', 'recover', 'approver'), 1)})


def test_resource_v3_preserves_input():
    value = resource()
    before = copy.deepcopy(value)
    assert validate_record(value, 'resource') == before
    assert value == before


@pytest.mark.parametrize('change', [dict(format_version=True), dict(audit_format='FIXED_LOG_V1'),
                                  dict(hard_limit_profile_id='a'*32)])
def test_mixed_schema_rejected(change):
    with pytest.raises(ValueError):
        validate_record(dict(resource(), **change), 'resource')


def test_duplicate_role_profile_rejected():
    value = resource()['hard_limit_profiles']
    value['publisher'] = value['creator'].copy()
    with pytest.raises(ValueError):
        validate_mapping(value)


def test_missing_role_rejected():
    value = resource()['hard_limit_profiles']
    del value['recover']
    with pytest.raises(ValueError):
        validate_mapping(value)
