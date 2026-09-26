"""The authenticated inventory route must fail closed when ownership cannot be resolved."""
from urllib.parse import urlsplit


def test_active_inventory_route_returns_503_not_an_empty_count(monkeypatch):
    from api import routes, profiles
    calls = []
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_active_webui_session_inventory", lambda profile: (_ for _ in ()).throw(RuntimeError("unavailable")))
    monkeypatch.setattr(routes, "bad", lambda handler, message, status=400: calls.append((status, message)))
    monkeypatch.setattr(routes, "j", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("false zero")))
    routes.handle_get(object(), urlsplit("/api/sessions/active"))
    assert calls == [(503, "Active task status unavailable")]
