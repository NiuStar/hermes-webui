"""Fixed-log resource and transition budgets; no OS admission authority."""
from api.display_bootstrap_audit_codec import file_bytes

# Required before writing the next business record, including failure reserve.
_REQUIRED = {'RESERVED': 8, 'BUILDING': 7, 'VERIFIED': 5, 'APPROVED': 4,
             'PUBLISH_INTENT': 3, 'PUBLISHED_UNACTIVATED': 0,
             'FAILED': 0, 'QUARANTINED': 0}


def validate_layout(resource):
    if (type(resource) is not dict or type(resource.get('format_version')) is not int
            or (resource['format_version'], resource.get('audit_format')) not in
               ((2, 'FIXED_LOG_V1'), (3, 'FIXED_LOG_V2'))):
        raise ValueError('INVALID_INPUT')
    slots = resource.get('audit_slot_count')
    size = file_bytes(slots)
    if (type(resource.get('audit_reserve_bytes')) is not int
            or resource['audit_reserve_bytes'] != size):
        raise ValueError('INVALID_INPUT')
    return dict(slot_count=slots, file_bytes=size)


def require_slots(snapshot, *, operation):
    """Keep full-container verification separate from capacity decisions.

    The snapshot must originate from a current complete scan held under lock.
    Completion readback never reserves slots. Failed audit writes never become
    persisted terminal states simply because this function returned a budget.
    """
    if type(snapshot) is not dict or operation not in ('advance', 'failure', 'readback'):
        raise ValueError('INVALID_INPUT')
    remaining = snapshot.get('remaining_slots')
    if type(remaining) is not int or not 0 <= remaining <= 4096:
        raise ValueError('INVALID_INPUT')
    last = snapshot.get('registry_last')
    if type(last) is not dict or last.get('state') not in _REQUIRED:
        raise ValueError('STATE_CONFLICT')
    state = last['state']
    terminal = state in ('PUBLISHED_UNACTIVATED', 'FAILED', 'QUARANTINED')
    if operation == 'readback':
        if not terminal:
            raise ValueError('STATE_CONFLICT')
        required = 0
    elif terminal:
        raise ValueError('STATE_CONFLICT')
    elif operation == 'failure':
        required = 1 if snapshot.get('pending_failure') is not None else 2
    else:
        if snapshot.get('pending_failure') is not None:
            raise ValueError('STATE_CONFLICT')
        required = _REQUIRED[state]
        if state == 'BUILDING' and snapshot.get('platform_sha') is not None:
            required -= 1  # Platform evidence has already consumed its slot.
    if remaining < required:
        raise ValueError('AUDIT_UNAVAILABLE')
    return required
