"""Context admission on the explicitly provisioned test host."""
import importlib.util
from pathlib import Path
import pytest


def test_context_refuses_untrusted_ancestor(tmp_path):
    import api.display_bootstrap_policy as policy
    assert hasattr(policy, 'open_protected_root'), 'protected directory walker missing'
    tmp_path.chmod(0o777)
    with pytest.raises(policy.BootstrapRejected) as caught:
        policy.open_protected_root(str(tmp_path), {0})
    assert caught.value.code == 'ACCESS_BOUNDARY_UNPROVEN'


def test_missing_policy_no_creation():
    assert importlib.util.find_spec('api.display_bootstrap_policy'), 'context admission missing'
    from api.display_bootstrap_policy import acquire_bootstrap_context, BootstrapRejected
    root = Path('/etc/hermes-display-bootstrap')
    before = sorted(p.name for p in root.iterdir())
    with pytest.raises(BootstrapRejected) as caught:
        acquire_bootstrap_context('a' * 32)
    assert caught.value.code == 'ACCESS_BOUNDARY_UNPROVEN'
    assert sorted(p.name for p in root.iterdir()) == before


@pytest.mark.parametrize('policy_id', [None, True, '', '../bad', 'A'*32, 'a'*31])
def test_invalid_policy_id(policy_id):
    from api.display_bootstrap_policy import acquire_bootstrap_context, BootstrapRejected
    with pytest.raises(BootstrapRejected) as caught:
        acquire_bootstrap_context(policy_id)
    assert caught.value.code == 'INVALID_INPUT'
