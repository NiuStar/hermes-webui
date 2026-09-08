"""Lifecycle checks for the real-HTTP, test-owned Sprint 3 skills profile."""
import pytest

from tests import test_sprint3 as sprint3
from tests.conftest import TEST_STATE_DIR


def test_owned_skills_releases_profile_without_switching_default(test_server):
    before, status = sprint3.get('/api/profiles')
    assert status == 200
    profiles = TEST_STATE_DIR / 'profiles'
    existing = set(profiles.iterdir()) if profiles.exists() else set()
    fixture = sprint3.owned_skills.__wrapped__(test_server)
    try:
        owned = next(fixture)
        created = set(profiles.iterdir()) - existing
        assert len(created) == 1
        for path in created:
            assert not path.is_symlink()
            assert len(list(path.glob('skills/*/*/SKILL.md'))) == 2
        sprint3.test_skills_list(owned)
        sprint3.test_skills_content_known(owned)
        # Failure paths must release exactly the same owned directory.
        with pytest.raises(RuntimeError, match='fixture consumer failed'):
            fixture.throw(RuntimeError('fixture consumer failed'))
    finally:
        fixture.close()
    assert set(profiles.iterdir()) == existing
    after, status = sprint3.get('/api/profiles')
    assert status == 200
    assert after['active'] == before['active']
