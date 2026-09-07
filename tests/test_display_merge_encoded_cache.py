"""Oversized historical merges stay bounded without dropping rows."""
import pytest
from api import routes
from tests.test_display_merge_cache_shortcut import _Session


@pytest.fixture(autouse=True)
def stable_key(monkeypatch):
    routes._display_merge_cache.clear()
    monkeypatch.setattr('api.models._sidecar_stat_signature', lambda p: ('sig', 1, 2, 3))
    monkeypatch.setattr(routes, '_state_db_session_signature', lambda *a, **k: 'SIG-A')
    yield 'SIG-A'
    routes._display_merge_cache.clear()


def test_oversized_merge_is_cached_losslessly(monkeypatch, stable_key):
    monkeypatch.setattr(routes, '_DISPLAY_MERGE_CACHE_MAX_BYTES', 4096)
    session = _Session()
    side = [{'role': 'user', 'content': 'hello', 'timestamp': 1}]
    rows = [{'role': 'assistant', 'content': 'large result ' * 10000,
             'timestamp': 2, 'metadata': {'nested': ['original']}}]
    merged = routes._limited_webui_messages_for_display_with_sidecar(
        session, side, rows, state_db_signature=stable_key)
    cached = routes._display_merge_cached_messages(session, side)
    assert cached == merged
    entry = routes._display_merge_cache[session.session_id]
    assert entry['size_bytes'] <= 4096
    assert 'messages' not in entry
    cached[-1]['metadata']['nested'].append('mutated')
    assert routes._display_merge_cached_messages(session, side) == merged


def test_expanded_cache_ceiling_rejects_entry(monkeypatch, stable_key):
    monkeypatch.setattr(routes, '_DISPLAY_MERGE_CACHE_MAX_BYTES', 64)
    monkeypatch.setattr(routes, '_DISPLAY_MERGE_CACHE_MAX_EXPANDED_BYTES', 128)
    session = _Session()
    side = [{'role': 'user', 'content': 'x' * 1000, 'timestamp': 1}]
    rows = [{'role': 'assistant', 'content': 'y', 'timestamp': 2}]
    result = routes._limited_webui_messages_for_display_with_sidecar(
        session, side, rows, state_db_signature=stable_key)
    assert len(result) == 2
    assert routes._display_merge_cached_messages(session, side) is None



def test_provenance_change_during_encoding_is_not_published(monkeypatch, stable_key):
    session = _Session()
    side = [{'role': 'user', 'content': 'hi', 'timestamp': 1}]
    rows = [{'role': 'assistant', 'content': 'bye', 'timestamp': 2}]
    original = routes._display_merge_cache_value
    def changed(messages):
        value = original(messages)
        monkeypatch.setattr(routes, '_state_db_session_signature', lambda *a, **k: 'CHANGED')
        return value
    monkeypatch.setattr(routes, '_display_merge_cache_value', changed)
    result = routes._limited_webui_messages_for_display_with_sidecar(
        session, side, rows, state_db_signature=stable_key)
    assert len(result) == 2
    assert session.session_id not in routes._display_merge_cache


def test_non_json_types_do_not_enter_encoded_cache(monkeypatch):
    monkeypatch.setattr(routes, '_DISPLAY_MERGE_CACHE_MAX_BYTES', 100)
    assert routes._display_merge_cache_value([{'content': 'x' * 1000, 'tuple': (1, 2)}]) is None
