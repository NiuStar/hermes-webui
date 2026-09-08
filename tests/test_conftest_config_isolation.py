"""Regression for disk-cache ownership across function fixture teardown."""
import copy

import pytest

import api.config as config
import tests.conftest as conftest


@pytest.mark.parametrize("fail_in_body", [False, True])
def test_config_boundary_restores_disk_identity_and_nested_values(tmp_path, fail_in_body):
    # Exercise the real autouse fixture boundary, including monkeypatch teardown.
    boundary = conftest._invalidate_models_cache_after_test.__wrapped__(None, None)
    next(boundary)
    original_cache = config._cfg_cache
    original_cfg = config.cfg
    snapshot = copy.deepcopy(config._cfg_cache)
    identity = (config._cfg_path, config._cfg_mtime, config._cfg_fingerprint)
    path = tmp_path / "config.yaml"
    path.write_text("model:\n  default: temporary-model\n  provider: local\n")
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(config, "_get_config_path", lambda: path)
            config.reload_config()
            assert config.get_config()["model"]["default"] == "temporary-model"
            # Runtime path changes must still defeat a same-test memory override.
            other = tmp_path / "other.yaml"
            other.write_text("model:\n  default: other-model\n")
            config._cfg_cache["model"]["default"] = "memory-override"
            patch.setattr(config, "_get_config_path", lambda: other)
            assert config.get_config()["model"]["default"] == "other-model"
            if fail_in_body:
                raise RuntimeError("simulated test failure")
    except RuntimeError as exc:
        assert str(exc) == "simulated test failure"
    finally:
        with pytest.raises(StopIteration):
            next(boundary)
    assert config._cfg_cache is original_cache
    assert config.cfg is original_cfg
    assert config._cfg_cache == snapshot
    assert (config._cfg_path, config._cfg_mtime, config._cfg_fingerprint) == identity
    # A later model fixture may safely install its own in-memory config.
    with pytest.MonkeyPatch.context() as patch:
        override = {"model": {"default": "next-test-model"}}
        patch.setattr(config, "cfg", override)
        assert config.get_config() is override


def test_config_boundary_restores_independent_cfg_contents(monkeypatch):
    override = {"model": {"default": "original"}}
    monkeypatch.setattr(config, "cfg", override)
    boundary = conftest._invalidate_models_cache_after_test.__wrapped__(None, None)
    next(boundary)
    try:
        config.cfg["model"]["default"] = "leaked"
    finally:
        with pytest.raises(StopIteration):
            next(boundary)
    assert config.cfg is override
    assert override == {"model": {"default": "original"}}
