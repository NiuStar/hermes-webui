"""Internal preparation ownership. No public constructor grants authority."""
import os
from contextlib import ExitStack
from api.display_bootstrap_audit_log import _deadline

_PREPARATIONS = {}
_RECEIPTS = {}


class _Preparation:
    def __init__(self):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')

    def __reduce__(self):
        raise TypeError('preparation cannot be serialized')

    def close(self):
        _PREPARATIONS.pop(id(self), None)
        for key, entry in tuple(_RECEIPTS.items()):
            if entry[1] is self:
                _RECEIPTS.pop(key, None)
        if not self.closed:
            self.closed = True
            self.owner.close()


class _Receipt:
    def __init__(self):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')

    def __reduce__(self):
        raise TypeError('receipt cannot be serialized')


def _new_preparation(deadline):
    _deadline(deadline)
    value = object.__new__(_Preparation)
    value.pid, value.deadline = os.getpid(), deadline
    value.owner, value.closed, value.consumed = ExitStack(), False, False
    _PREPARATIONS[id(value)] = value
    return value


def _check_preparation(value):
    if (type(value) is not _Preparation or _PREPARATIONS.get(id(value)) is not value
            or value.closed or value.consumed or value.pid != os.getpid()):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    _deadline(value.deadline)


def _register_receipt(value, verified):
    _check_preparation(value)
    if any(entry[1] is value for entry in _RECEIPTS.values()):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    receipt = object.__new__(_Receipt)
    _RECEIPTS[id(receipt)] = receipt, value, verified
    return receipt


def _consume_receipt(receipt, verify, construct):
    # Pop before any operation: failure must never resurrect this receipt.
    entry = _RECEIPTS.pop(id(receipt), None)
    if type(receipt) is not _Receipt or entry is None or entry[0] is not receipt:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    _, preparation, verified = entry
    owner = None
    try:
        _check_preparation(preparation)
        verify(preparation, verified)
        owner = preparation.owner.pop_all()
        preparation.consumed = True
        _PREPARATIONS.pop(id(preparation), None)
        result = construct(preparation, verified, owner)
        owner = None
        preparation.closed = True
        # The transferred owner is the sole closer; erase stale capability refs.
        for key in tuple(vars(preparation)):
            if key not in ('pid', 'deadline', 'closed', 'consumed', 'owner'):
                delattr(preparation, key)
        return result
    except BaseException:
        if owner is not None:
            owner.close()
        else:
            preparation.close()
        raise
