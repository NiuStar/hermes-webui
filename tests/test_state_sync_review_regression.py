import pytest


@pytest.mark.parametrize('entry', ['sync_session_title', 'sync_session_usage'])
@pytest.mark.parametrize('title', ['   ', '\u200b'])
def test_semantic_empty_preserves_derived_title(tmp_path, monkeypatch, entry, title):
    from api import state_sync
    from hermes_state import SessionDB
    path = tmp_path / 'state.db'
    db = SessionDB(path)
    try:
        db.ensure_session(session_id='derived', source='cli')
        db.set_auto_title('derived', 'Derived Name', source=db.TITLE_SOURCE_DERIVED)
        monkeypatch.setattr(state_sync, '_get_state_db', lambda profile=None: SessionDB(path))
        getattr(state_sync, entry)('derived', title=title)
        assert db.get_session_title('derived') == 'Derived Name'
        assert db.get_session_title_source('derived') == db.TITLE_SOURCE_DERIVED
    finally:
        db.close()


def test_usage_sync_preserves_manual_title(tmp_path, monkeypatch):
    from api import state_sync
    from hermes_state import SessionDB
    path = tmp_path / 'state.db'
    db = SessionDB(path)
    try:
        db.ensure_session(session_id='manual', source='cli')
        db.set_session_title('manual', 'Manual Name')
        monkeypatch.setattr(state_sync, '_get_state_db', lambda profile=None: SessionDB(path))
        state_sync.sync_session_usage('manual', title='Stale WebUI Title')
        assert db.get_session_title('manual') == 'Manual Name'
    finally:
        db.close()


def test_title_retries_second_collision(tmp_path, monkeypatch):
    from api import state_sync
    from hermes_state import SessionDB
    path = tmp_path / 'state.db'
    db = SessionDB(path)
    real = state_sync._set_auto_title_if_empty
    calls = []
    def collision(handle, sid, title):
        calls.append(title)
        if len(calls) <= 2:
            other = 'other-' + str(len(calls))
            handle.ensure_session(session_id=other, source='cli')
            handle.set_session_title(other, title)
        return real(handle, sid, title)
    try:
        monkeypatch.setattr(state_sync, '_get_state_db', lambda profile=None: SessionDB(path))
        monkeypatch.setattr(state_sync, '_set_auto_title_if_empty', collision)
        state_sync.sync_session_title('target', 'Same Title')
        assert db.get_session_title('target') == 'Same Title #3'
        assert len(calls) == 3
    finally:
        db.close()
