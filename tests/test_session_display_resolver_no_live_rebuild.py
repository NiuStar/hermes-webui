"""Regression: GET /api/session display resolvers must never trigger the
live provider-catalog rebuild.

Root cause (multi-tab streaming interlock RCA, task t_d127953d):
``_resolve_effective_session_model_for_display`` /
``_resolve_effective_session_model_provider_for_display`` are called by the
hot, side-effect-free ``GET /api/session?...&resolve_model=1`` path. When a
session has no persisted ``model_provider`` (common — e.g. kanban/imported
sessions), the fast path in ``_resolve_compatible_session_model_state`` is
skipped and the resolver fell through to ``get_available_models()`` WITHOUT
``prefer_cache``. On a non-AWS / WSL / corp network that cold rebuild blocks
~10s on a botocore IMDS probe (plus anthropic/openrouter /models) and, run
concurrently across browser tabs, serializes on the models-cache lock and
starves SSE/streaming -> BrokenPipe/Cancelled storm.

This is an INVARIANT test, not a change-detector: it asserts the resolvers
resolve from the cache-only path and never reach the live-rebuild seam
``api.config._invoke_models_rebuild`` — regardless of whether the session
carries a model_provider.
"""

import pytest

import api.config as cfg
import api.routes as routes


class _FakeSession:
    """Minimal stand-in for a Session row as seen by the display resolvers."""

    def __init__(self, model, model_provider):
        self.model = model
        self.model_provider = model_provider


@pytest.fixture
def cold_models_cache(monkeypatch):
    """Force a cold in-memory + disk models cache without touching real state.

    Cold cache is what makes the regression observable: a warm cache short-
    circuits before any rebuild decision, hiding the prefer_cache contract.
    """
    # A different active family must not rewrite historical display metadata.
    monkeypatch.setattr(cfg, "cfg", {"model": {"provider": "ollama", "default": "qwen3:8b"}})
    monkeypatch.setattr(cfg, "_cfg_path", cfg._get_config_path())
    monkeypatch.setattr(cfg, "_cfg_mtime", 0.0)
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda *_: (None, None, None))
    monkeypatch.setattr(cfg, "_available_models_cache", None, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", 0.0, raising=False)
    monkeypatch.setattr(
        cfg, "_available_models_cache_source_fingerprint", None, raising=False
    )
    monkeypatch.setattr(cfg, "_cache_build_in_progress", False, raising=False)
    # Never read/write the real on-disk cache during the test.
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", lambda *_a, **_k: None)
    monkeypatch.setattr(cfg, "_delete_models_cache_on_disk", lambda: None)
    yield


@pytest.fixture
def rebuild_seam_tripwire(monkeypatch):
    """Make the live provider-catalog rebuild seam fail loudly if reached.

    ``_invoke_models_rebuild`` is the documented indirection seam around the
    cold, network-touching per-provider rebuild. The display resolvers must
    never reach it (prefer_cache returns the network-free minimal catalog
    *before* this seam). If a future edit drops ``prefer_cached_catalog=True``,
    the resolver falls into the cold rebuild and trips this wire.
    """
    calls = {"n": 0}

    def _boom(_builder):
        calls["n"] += 1
        raise AssertionError(
            "live provider-catalog rebuild ran on the hot GET /api/session "
            "display path — prefer_cached_catalog regression"
        )

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _boom)
    return calls


@pytest.mark.parametrize(
    "model_provider",
    [None, "", "anthropic"],
    ids=["no-provider", "empty-provider", "with-provider"],
)
def test_session_display_resolvers_never_trigger_live_rebuild(
    cold_models_cache, rebuild_seam_tripwire, model_provider
):
    session = _FakeSession("claude-opus-4-7", model_provider)

    # Must not raise (the tripwire raises AssertionError if the live rebuild
    # path is entered) and must return the persisted model verbatim.
    model = routes._resolve_effective_session_model_for_display(session)
    provider = routes._resolve_effective_session_model_provider_for_display(session)

    assert model == "claude-opus-4-7"
    assert session.model == model
    assert session.model_provider == model_provider
    # provider is best-effort; the contract under test is "no live rebuild",
    # not a specific provider string. It must at least be None or a str.
    assert provider is None or isinstance(provider, str)
    assert rebuild_seam_tripwire["n"] == 0


@pytest.mark.parametrize("model", [None, ""])
def test_missing_model_uses_cache_only_default(
    cold_models_cache, rebuild_seam_tripwire, model
):
    session = _FakeSession(model, None)
    assert routes._resolve_effective_session_model_for_display(session) == "qwen3:8b"
    routes._resolve_effective_session_model_provider_for_display(session)
    assert session.model == model
    assert session.model_provider is None
    assert rebuild_seam_tripwire["n"] == 0
