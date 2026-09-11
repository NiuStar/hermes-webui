"""Real private SQLite construction; does not certify context admission."""
import os
import time
import pytest
from api import display_bootstrap_artifact as artifact


def test_build_freezes_real_empty_database(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        result = artifact.build_private_database(fd, max_bytes=1048576, deadline=time.monotonic()+10)
        assert set(os.listdir(fd)) == {'display.sqlite'}
        assert result == artifact.verify_frozen_database(fd, max_bytes=1048576, deadline=time.monotonic()+10)
        original = (tmp_path/'display.sqlite').read_bytes()
        with pytest.raises(ValueError, match='TARGET_CONFLICT'):
            artifact.build_private_database(fd, max_bytes=1048576, deadline=time.monotonic()+10)
        assert (tmp_path/'display.sqlite').read_bytes() == original
    finally:
        os.close(fd)


@pytest.mark.parametrize('expired,budget', [(True,1048576),(False,1)])
def test_build_rejects_invalid_budget_without_creation(tmp_path, expired, budget):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError, match='RESOURCE_LIMIT'):
            artifact.build_private_database(fd,max_bytes=budget,deadline=time.monotonic()+(-1 if expired else 10))
        assert os.listdir(fd) == []
    finally:
        os.close(fd)
