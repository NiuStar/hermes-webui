"""Linux cgroup observations: fail closed outside a bounded executor."""
from pathlib import Path
import pytest
from api import display_bootstrap_policy as policy


def test_unbounded_process_is_rejected():
    with pytest.raises(policy.BootstrapRejected, match='RESOURCE_LIMIT'):
        policy.verify_memory_limit(67108864)


def test_memory_limit_input_validation():
    for value in [True,0,-1,'67108864',None]:
        with pytest.raises(policy.BootstrapRejected, match='INVALID_INPUT'):
            policy.verify_memory_limit(value)
