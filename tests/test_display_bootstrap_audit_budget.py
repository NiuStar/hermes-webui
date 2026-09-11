import pytest

from api.display_bootstrap_audit_budget import require_slots, validate_layout


def snapshot(state, remaining):
    return dict(registry_last={'state': state}, remaining_slots=remaining,
                pending_failure=None)


@pytest.mark.parametrize('state,required', [('RESERVED', 8), ('BUILDING', 7),
                                           ('VERIFIED', 5), ('APPROVED', 4),
                                           ('PUBLISH_INTENT', 3)])
def test_advance_reserve(state, required):
    assert require_slots(snapshot(state, required), operation='advance') == required
    with pytest.raises(ValueError, match='AUDIT_UNAVAILABLE'):
        require_slots(snapshot(state, required - 1), operation='advance')


@pytest.mark.parametrize('state', ['PUBLISHED_UNACTIVATED', 'FAILED', 'QUARANTINED'])
def test_terminal_needs_no_empty_slots(state):
    assert require_slots(snapshot(state, 0), operation='readback') == 0
    with pytest.raises(ValueError):
        require_slots(snapshot(state, 9), operation='advance')


def test_pending_failure_only_allows_completion():
    current = snapshot('BUILDING', 1)
    current['pending_failure'] = {'error_code': 'IO_FAILURE'}
    assert require_slots(current, operation='failure') == 1
    with pytest.raises(ValueError, match='STATE_CONFLICT'):
        require_slots(current, operation='advance')


def test_budget_version_and_exact_bytes():
    policy = dict(format_version=2, audit_format='FIXED_LOG_V1',
                  audit_slot_count=9, audit_reserve_bytes=667648)
    assert validate_layout(policy)['file_bytes'] == 667648
    for changes in [dict(format_version=1), dict(audit_slot_count=True),
                    dict(audit_reserve_bytes=589824), dict(audit_format='legacy')]:
        with pytest.raises(ValueError):
            validate_layout(dict(policy, **changes))
