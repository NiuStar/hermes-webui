"""Docker's image-owned Agent may be detected but must not be installed from a tag."""
from unittest.mock import patch

import pytest

from api import updates

_REAL_BAKED_AGENT_PATH_OVERRIDDEN = updates._baked_agent_path_overridden


@pytest.fixture(autouse=True)
def simulated_baked_image(monkeypatch):
    """Temp dirs model /opt/hermes unless explicitly modelling an override."""
    monkeypatch.setattr(updates, '_baked_agent_path_overridden', lambda _path: False)


def _agent(tmp_path, revision='a' * 40):
    agent = tmp_path / 'agent'
    agent.mkdir()
    (agent / '.hermes-agent-revision').write_text(revision + '\n')
    return agent


def test_baked_agent_detects_published_newer_commit_without_git(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    monkeypatch.setattr(updates, 'deployment_info', lambda: {'type': 'docker', 'online_update': True})
    with patch.object(updates, '_published_agent_release', return_value=('v0.21.6', 'b' * 40)), \
         patch.object(updates, '_agent_commit_comparison', return_value=('ahead', 7)):
        result = updates._check_repo(agent, 'agent')
    assert result['current_sha'] == 'a' * 40
    assert result['latest_sha'] == 'b' * 40
    assert result['latest_version'] == 'v0.21.6'
    assert result['behind'] == 7
    assert result['image_managed'] is True
    assert result['manual_update'] is True
    assert result['deployment_online_update'] is False
    assert 'webui_image_version' not in result


def test_baked_agent_never_offers_tag_based_online_update(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    monkeypatch.setattr(updates, 'deployment_info', lambda: {'type': 'docker', 'online_update': True})
    with patch.object(updates, '_published_agent_release', return_value=('v0.21.6', 'b' * 40)), \
         patch.object(updates, '_agent_commit_comparison', return_value=('ahead', 3)):
        result = updates._check_repo(agent, 'agent')
    assert result['behind'] == 3
    assert result['deployment_online_update'] is False


def test_signed_candidate_must_match_verified_official_latest(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    monkeypatch.setattr(updates, 'deployment_info', lambda: {'type': 'docker', 'online_update': True})
    monkeypatch.setattr(updates, 'request_agent_preflight',
                        lambda: {'ok': True, 'latest_sha': 'c' * 40, 'latest_version': 'v0.21.5'})
    with patch.object(updates, '_published_agent_release', return_value=('v0.21.6', 'b' * 40)), \
         patch.object(updates, '_agent_commit_comparison', return_value=('ahead', 7)):
        result = updates._check_baked_agent_release(agent)
    assert result['behind'] == 7
    assert result['deployment_online_update'] is False


def test_signed_candidate_matching_official_latest_is_installable(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    monkeypatch.setattr(updates, 'deployment_info', lambda: {'type': 'docker', 'online_update': True})
    monkeypatch.setattr(updates, 'request_agent_preflight',
                        lambda: {'ok': True, 'latest_sha': 'b' * 40, 'latest_version': 'v0.21.6'})
    with patch.object(updates, '_published_agent_release', return_value=('v0.21.6', 'b' * 40)), \
         patch.object(updates, '_agent_commit_comparison', return_value=('ahead', 7)):
        result = updates._check_baked_agent_release(agent)
    assert result['deployment_online_update'] is True


def test_direct_agent_apply_refuses_signed_manifest_that_is_not_official_latest(monkeypatch):
    monkeypatch.setattr(updates, 'deployment_info', lambda: {'type': 'docker', 'online_update': True})
    monkeypatch.setattr(updates, '_restart_blocker_snapshot', lambda: {'restart_blocked': False})
    monkeypatch.setattr(updates, '_published_agent_release', lambda: ('v0.21.6', 'b' * 40))
    monkeypatch.setattr(updates, 'request_agent_preflight',
                        lambda: {'ok': True, 'latest_sha': 'c' * 40, 'latest_version': 'v0.21.5'})
    monkeypatch.setattr(updates, 'request_agent_update',
                        lambda: pytest.fail('signed mismatch must not start update'))
    assert updates.apply_docker_agent_update()['ok'] is False


def test_direct_agent_apply_refuses_unverifiable_official_latest(monkeypatch):
    monkeypatch.setattr(updates, 'deployment_info', lambda: {'type': 'docker', 'online_update': True})
    monkeypatch.setattr(updates, '_restart_blocker_snapshot', lambda: {'restart_blocked': False})
    monkeypatch.setattr(updates, '_published_agent_release',
                        lambda: (_ for _ in ()).throw(TimeoutError('offline')))
    monkeypatch.setattr(updates, 'request_agent_update',
                        lambda: pytest.fail('unknown official release must not start update'))
    assert updates.apply_docker_agent_update()['ok'] is False


def test_same_commit_is_up_to_date_without_comparison(tmp_path):
    agent = _agent(tmp_path, 'b' * 40)
    with patch.object(updates, '_published_agent_release', return_value=('v0.21.6', 'b' * 40)), \
         patch.object(updates, '_agent_commit_comparison') as compare:
        result = updates._check_baked_agent_release(agent)
    compare.assert_not_called()
    assert result['behind'] == 0


def test_same_commit_never_offers_signed_install(tmp_path, monkeypatch):
    agent = _agent(tmp_path, 'b' * 40)
    monkeypatch.setattr(updates, 'deployment_info', lambda: {'type': 'docker', 'online_update': True})
    monkeypatch.setattr(updates, 'request_agent_preflight',
                        lambda: {'ok': True, 'latest_sha': 'b' * 40, 'latest_version': 'v0.21.6'})
    with patch.object(updates, '_published_agent_release', return_value=('v0.21.6', 'b' * 40)):
        result = updates._check_baked_agent_release(agent)
    assert result['behind'] == 0
    assert result['deployment_online_update'] is False


def test_same_commit_direct_apply_does_not_call_sidecar(monkeypatch, tmp_path):
    agent = _agent(tmp_path, 'b' * 40)
    monkeypatch.setattr(updates, '_AGENT_DIR', agent)
    monkeypatch.setattr(updates, 'deployment_info', lambda: {'type': 'docker', 'online_update': True})
    monkeypatch.setattr(updates, '_restart_blocker_snapshot', lambda: {'restart_blocked': False})
    monkeypatch.setattr(updates, '_published_agent_release', lambda: ('v0.21.6', 'b' * 40))
    monkeypatch.setattr(updates, 'request_agent_preflight',
                        lambda: {'ok': True, 'latest_sha': 'b' * 40, 'latest_version': 'v0.21.6'})
    monkeypatch.setattr(updates, 'request_agent_update',
                        lambda: pytest.fail('same Agent commit must never trigger replacement'))
    assert updates.apply_docker_agent_update()['ok'] is False


@pytest.mark.parametrize('comparison', [('unknown', 0), ('identical', 0)])
def test_unverified_commit_relation_is_not_an_update(tmp_path, comparison):
    agent = _agent(tmp_path)
    with patch.object(updates, '_published_agent_release', return_value=('v0.21.6', 'b' * 40)), \
         patch.object(updates, '_agent_commit_comparison', return_value=comparison):
        result = updates._check_baked_agent_release(agent)
    assert result['behind'] is None
    assert result['error']
    assert result['deployment_online_update'] is False


def test_invalid_revision_fails_closed(tmp_path):
    agent = _agent(tmp_path, 'unknown')
    with patch.object(updates, '_published_agent_release') as release:
        result = updates._check_baked_agent_release(agent)
    release.assert_not_called()
    assert result['behind'] is None
    assert result['error']


def test_network_failure_is_unknown_not_up_to_date(tmp_path):
    agent = _agent(tmp_path)
    with patch.object(updates, '_published_agent_release', side_effect=TimeoutError):
        result = updates._check_baked_agent_release(agent)
    assert result['behind'] is None
    assert result['error']


def test_no_git_non_docker_agent_remains_uncheckable(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    monkeypatch.setattr(updates, 'deployment_info', lambda: {'type': 'binary', 'online_update': False})
    assert updates._check_repo(agent, 'agent')['behind'] is None


def test_docker_agent_without_revision_is_unknown(tmp_path, monkeypatch):
    agent = tmp_path / 'agent'
    agent.mkdir()
    monkeypatch.setattr(updates, 'deployment_info', lambda: {'type': 'docker', 'online_update': True})
    result = updates._check_baked_agent_release(agent)
    assert result['behind'] is None
    assert result['image_managed'] is True
    assert result['error']


def test_override_does_not_claim_image_revision(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    monkeypatch.setattr(updates, 'deployment_info', lambda: {'type': 'docker', 'online_update': True})
    monkeypatch.setattr(updates, '_baked_agent_path_overridden', lambda _path: True)
    with patch.object(updates, '_published_agent_release') as release:
        result = updates._check_repo(agent, 'agent')
    release.assert_not_called()
    assert result['behind'] is None
    assert result['image_managed'] is True
    assert result['error']


def test_baked_identity_rejects_override_and_mount():
    assert _REAL_BAKED_AGENT_PATH_OVERRIDDEN('/tmp/custom-agent') is True
    with patch('pathlib.Path.read_text', return_value='11 2 0:2 / /opt/hermes rw - ext4 /dev/null rw\n'):
        assert _REAL_BAKED_AGENT_PATH_OVERRIDDEN('/opt/hermes') is True
    with patch('pathlib.Path.read_text', return_value='11 2 0:2 / /opt/hermes/agent rw - ext4 /dev/null rw\n'):
        assert _REAL_BAKED_AGENT_PATH_OVERRIDDEN('/opt/hermes') is True
    with patch('pathlib.Path.read_text', return_value='11 2 0:2 / /opt rw - ext4 /dev/null rw\n'):
        assert _REAL_BAKED_AGENT_PATH_OVERRIDDEN('/opt/hermes') is True


def test_release_page_commit_is_read_from_header(monkeypatch):
    commit = 'a' * 40
    monkeypatch.setattr(updates, '_agent_release_html',
                        lambda: ('v0.21.6', '<a href="/NousResearch/hermes-agent/releases/tag/v0.21.6">'
                                 '</a><a href="/NousResearch/hermes-agent/commit/' + commit + '">commit</a>'))
    assert updates._published_agent_release() == ('v0.21.6', commit)


def test_diverged_and_malformed_compare_are_unknown():
    assert updates._agent_commit_comparison('not-a-sha', 'b' * 40) == ('unknown', 0)
    assert updates._agent_commit_comparison('a' * 40, 'a' * 40) == ('identical', 0)


@pytest.mark.parametrize('path', ['/api/updates/apply', '/api/updates/force', '/api/updates/clear_lock'])
def test_docker_agent_write_routes_never_enter_git_or_image_updater(monkeypatch, path):
    from types import SimpleNamespace
    from api import routes

    handler = object()
    monkeypatch.setattr(routes, 'read_body', lambda _handler: {'target': 'agent'})
    monkeypatch.setattr(routes, '_check_csrf', lambda _handler: True)
    monkeypatch.setattr(routes, '_handle_extension_sidecar_proxy', lambda *_args, **_kwargs: False)
    monkeypatch.setattr(routes, '_guard_request_session_visibility', lambda *_args, **_kwargs: True)
    monkeypatch.setattr(updates, 'deployment_info', lambda: {'type': 'docker', 'online_update': True})
    monkeypatch.setattr(routes, 'j', lambda _handler, payload: payload)
    with patch.object(updates, 'apply_update') as git_apply, \
         patch.object(updates, 'apply_force_update') as git_force, \
         patch.object(updates, 'apply_clear_lock') as git_clear, \
         patch.object(updates, 'apply_docker_update') as image_apply:
        result = routes.handle_post(handler, SimpleNamespace(path=path))
    assert result['ok'] is False
    assert result['target'] == 'agent'
    git_apply.assert_not_called()
    git_force.assert_not_called()
    git_clear.assert_not_called()
    image_apply.assert_not_called()