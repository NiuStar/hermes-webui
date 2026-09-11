"""Use the real Agent approval entry across its module move; never fake state."""


def approval_entry(data):
    try:
        from tools.approval_gateway_wait import _ApprovalEntry
    except ImportError:
        from tools.approval import _ApprovalEntry
    return _ApprovalEntry(data)
