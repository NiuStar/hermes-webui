"""Typed bus decoder contracts; no claim of live observer qualification."""
import pytest
from api import display_bootstrap_systemd_bus as bus


@pytest.mark.parametrize('signature,value', [
    ('u', -1), ('u', 2**32), ('t', -1), ('t', 2**64),
    ('i', -2**31-1), ('i', 2**31), ('u', True), ('t', False),
    ('b', 1), ('s', []), ('ay', [True]), ('ay', [256]),
    ('as', [1]), ('(bas)', [1, []]), ('(bas)', [True, ['AF_UNIX', 1]]),
    ('(bas)', [True]), ('(bas)', [True, 'AF_UNIX']),
])
def test_invalid_data_rejected(monkeypatch, signature, value):
    monkeypatch.setattr(bus, 'call', lambda *args: dict(type=signature, data=value))
    with pytest.raises(ValueError, match='ACCESS_BOUNDARY_UNPROVEN'):
        bus.property_value('/unit', 'Service', 'Property', signature, 1)


@pytest.mark.parametrize('signature,value', [
    ('u', 0), ('u', 2**32-1), ('t', 2**64-1),
    ('i', -2**31), ('i', 2**31-1), ('b', False),
    ('ay', [0, 255]), ('as', ['AF_UNIX']), ('(bas)', [True, ['AF_UNIX']]),
])
def test_typed_boundaries(monkeypatch, signature, value):
    monkeypatch.setattr(bus, 'call', lambda *args: dict(type=signature, data=value))
    assert bus.property_value('/unit', 'Service', 'Property', signature, 1) == value


def test_signature_mismatch(monkeypatch):
    monkeypatch.setattr(bus, 'call', lambda *args: dict(type='i', data=1))
    with pytest.raises(ValueError, match='ACCESS_BOUNDARY_UNPROVEN'):
        bus.property_value('/unit', 'Service', 'Property', 'u', 1)
