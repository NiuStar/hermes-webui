"""Decision matrix only; filesystem verification remains mandatory."""
import itertools
import pytest
from api import display_bootstrap_publish as p


@pytest.mark.parametrize('state,s,t,a', itertools.product(
    ['FAILED','QUARANTINED','RESERVED','BUILDING','VERIFIED','APPROVED',
     'PUBLISH_INTENT','PUBLISHED_UNACTIVATED'], [False,True], [False,True], [False,True]))
def test_recovery_matrix(state,s,t,a):
    if state == 'FAILED': expected = 'BLOCKED_TERMINAL'
    elif state == 'QUARANTINED': expected = 'QUARANTINED'
    elif state in ('RESERVED','BUILDING'): expected = 'FAIL_INTERRUPTED_BUILD'
    elif state == 'VERIFIED': expected = 'WAIT_EXPLICIT_APPROVAL'
    elif s == t or (state == 'PUBLISHED_UNACTIVATED' and s): expected = 'QUARANTINE_CONFLICT'
    elif state == 'APPROVED' and not s: expected = 'QUARANTINE_CONFLICT'
    elif not a: expected = 'BLOCKED_APPROVAL'
    elif state == 'APPROVED': expected = 'WRITE_INTENT'
    elif s: expected = 'RENAME'
    elif state == 'PUBLISH_INTENT': expected = 'SYNC_AND_COMPLETE'
    else: expected = 'READBACK_COMPLETED'
    assert p.recovery_action(state, source_exists=s, target_exists=t, approval_matches=a) == expected


def test_unknown_presence_is_not_absence():
    with pytest.raises(ValueError, match='IO_FAILURE'):
        p.recovery_action('PUBLISH_INTENT',source_exists=None,target_exists=True,approval_matches=True)
