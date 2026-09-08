"""Historical display identity is independent of current inference defaults."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import api.routes as routes

@pytest.mark.parametrize("model,provider,expected_provider", [
    ("claude-opus-4-7", None, None),
    ("claude-opus-4-7", "", None),
    ("@archived:claude-opus-4-7", None, "archived"),
    ("claude-opus-4-7", "anthropic", "anthropic"),
    ("openai/gpt-old", None, None),
    ("unknown-vendor/model:tag", None, None),
    ("org/model:free", "custom:proxy", "custom:proxy"),
    ("@custom:proxy:org/model:free", None, "custom:proxy"),
    ("@ollama:qwen3:8b", None, "ollama"),
    ("qwen3:8b", "custom:localhost:11434", "custom:localhost:11434"),
])
def test_historical_identity_not_rebound_to_current_profile(monkeypatch, model, provider, expected_provider):
    session = SimpleNamespace(model=model, model_provider=provider, save=Mock())
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda *_: ("ollama", "qwen3:8b", {}))
    monkeypatch.setattr(routes, "get_available_models", lambda prefer_cache=False: {"active_provider": "ollama", "default_model": "qwen3:8b", "groups": []})
    assert routes._resolve_effective_session_model_for_display(session) == model
    assert routes._resolve_effective_session_model_provider_for_display(session) == expected_provider
    assert (session.model, session.model_provider) == (model, provider)
    session.save.assert_not_called()


def test_session_response_keeps_historical_identity(monkeypatch):
    from tests.history_route_harness import session_probe
    session, request = session_probe(monkeypatch, query="messages=0&resolve_model=1")
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda *_: ("ollama", "qwen3:8b", {}))
    catalog = Mock(side_effect=AssertionError("persisted display must not need catalog"))
    monkeypatch.setattr(routes, "get_available_models", catalog)
    result = request()["session"]
    assert result["model"] == "claude-opus-4-7"
    assert result["model_provider"] is None
    assert session.model == "claude-opus-4-7"
    session.save.assert_not_called()
    catalog.assert_not_called()


def test_inference_compatibility_still_repairs_cross_family(monkeypatch):
    monkeypatch.setattr(routes, "get_available_models", lambda: {"active_provider": "ollama", "default_model": "qwen3:8b", "groups": []})
    model, _, changed = routes._resolve_compatible_session_model_state("claude-opus-4-7", None)
    assert model == "qwen3:8b"
    assert changed is True
