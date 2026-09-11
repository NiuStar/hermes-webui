import pytest
from api.display_bootstrap_storage_units import render


def test_observer_is_single_request_readonly_service():
    units = render('/opt/releases/bootstrap-01', '/usr/bin/python3', 1001)
    service = units['hermes-bootstrap-storage@.service']
    socket = units['hermes-bootstrap-storage.socket']
    assert 'Accept=yes\n' in socket
    assert 'SocketMode=0660\n' in socket
    assert 'SocketGroup=1001\n' in socket
    assert ' -I -B /opt/releases/bootstrap-01/scripts/bootstrap_storage_entry.py\n' in service
    assert 'ProtectSystem=strict\n' in service
    assert 'PrivateDevices=no\n' in service
    assert 'Restart=no\n' in service
    assert 'KillSignal=SIGKILL\n' in service


@pytest.mark.parametrize('path', ['/opt/x y', '/opt/../x', '/opt/$x', 'relative', '/opt//x'])
def test_unit_paths_cannot_inject(path):
    with pytest.raises(ValueError):
        render(path, '/usr/bin/python3', 1001)
