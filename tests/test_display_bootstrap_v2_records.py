"""Version migration codec tests; parsing is not context admission."""
import copy
import pytest
from api.display_bootstrap_manifest import validate_record


def resource():
    return dict(format_version=2, max_candidate_bytes=1048576, min_free_bytes=4096,
                max_retained_candidates=10, max_rss_bytes=67108864,
                max_elapsed_seconds=60, check_interval_ms=100,
                audit_reserve_bytes=667648, hard_limit_profile_id='a'*32,
                audit_format='FIXED_LOG_V1', audit_slot_count=9)


def test_v2_resource_does_not_modify_input():
    value = resource()
    before = copy.deepcopy(value)
    assert validate_record(value, 'resource') is value
    assert value == before


@pytest.mark.parametrize('changes', [dict(format_version=True), dict(format_version=3),
    dict(audit_slot_count=True), dict(audit_slot_count=8), dict(audit_reserve_bytes=65536),
    dict(audit_format='legacy'), dict(extra='ignored'), dict(max_elapsed_seconds=0)])
def test_v2_resource_rejects_invalid(changes):
    with pytest.raises(ValueError):
        validate_record(dict(resource(), **changes), 'resource')


def test_missing_v2_field_does_not_downgrade():
    value = resource()
    del value['audit_slot_count']
    with pytest.raises(ValueError):
        validate_record(value, 'resource')


def test_v1_cannot_smuggle_v2_fields():
    with pytest.raises(ValueError):
        validate_record(dict(resource(), format_version=1), 'resource')
