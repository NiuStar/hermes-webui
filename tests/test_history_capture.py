"""Read-only disk-source captures, not fake merged history fixtures."""
import importlib.util
import json
import sqlite3
import pytest


def test_all_pages_manifest_binding_and_tamper_detection(tmp_path):
    from api import history_capture as capture_module
    assert hasattr(capture_module, 'compare_capture'), 'complete differential runner missing'
    home = sources(tmp_path)
    child = home / 'sessions/child.json'
    data = json.loads(child.read_text())
    data['messages'] += [{'role': 'assistant', 'content': 'call', 'timestamp': 4,
                         'tool_calls': [{'id': 't'}]},
                        {'role': 'tool', 'content': 'x' * 5000, 'tool_call_id': 't'},
                        {'role': 'user', 'content': 'last', 'timestamp': 6}]
    data['tool_calls'] = [{'name': 'tool', 'assistant_msg_idx': 2, 'id': 't', 'snippet': 'result'}]
    data['anchor_activity_scenes'] = {'scene': {'message_index': 2, 'scene': {
        'schema': 'activity_scene_v1', 'items': [], 'final_text': 'call'}}}
    data['truncation_watermark'] = 6
    child.write_text(json.dumps(data))
    capture = capture_module.capture_sources(home / 'sessions', home / 'state.db', 'child',
                                              profile_identity='test-profile', max_bytes=100000)
    report = capture_module.compare_capture(capture, tmp_path / 'shadow.db', limits=[1, 2, 5], max_bytes=100000)
    assert report['status'] == 'PASS'
    assert report['routing'] == 'LEGACY'
    assert all(r['complete'] and r['pages'] > 0 for r in report['limits'])
    assert report['capture_sha256'] == capture['sha256']
    with sqlite3.connect(tmp_path / 'shadow.db') as db:
        bound = db.execute('SELECT capture_sha256 FROM shadow_captures').fetchone()[0]
        assert bound == capture['sha256']
    capture['sidecars']['child']['messages'][0]['content'] = 'tampered'
    with pytest.raises(ValueError, match='integrity'):
        capture_module.verify_capture(capture)


def test_cli_roundtrip_and_failure_exit(tmp_path):
    import subprocess
    import sys
    home = sources(tmp_path)
    output = tmp_path / 'capture.json'
    command = [sys.executable, '-m', 'api.history_capture']
    result = subprocess.run(command + ['capture', '--session-dir', str(home / 'sessions'),
        '--state-db', str(home / 'state.db'), '--session-id', 'child', '--profile-identity', 'test',
        '--max-bytes', '100000', '--output', str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert output.exists(), 'capture CLI did not create artifact'
    report = tmp_path / 'report.json'
    result = subprocess.run(command + ['compare', '--capture', str(output), '--output-db', str(tmp_path / 'shadow.db'),
        '--limits', '1,2', '--max-bytes', '100000', '--output', str(report)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(report.read_text())['status'] == 'PASS'
    output.write_text('{}')
    result = subprocess.run(command + ['compare', '--capture', str(output), '--output-db', str(tmp_path / 'bad.db'),
        '--limits', '1', '--max-bytes', '100000', '--output', str(tmp_path / 'bad.json')], capture_output=True, text=True)
    assert result.returncode != 0
    assert not (tmp_path / 'bad.db').exists()


def test_bound_generation_rejects_unrelated_messages(tmp_path):
    from api.history_capture import capture_sources
    from api.display_history import HistoryStore, ScopeKey
    home = sources(tmp_path)
    capture = capture_sources(home / 'sessions', home / 'state.db', 'child', profile_identity='test', max_bytes=100000)
    with pytest.raises(ValueError, match='capture payload'):
        HistoryStore(tmp_path / 'shadow.db').build_shadow(ScopeKey('test', 'child'), [], max_bytes=100000, capture=capture)


@pytest.mark.parametrize('case', ['cycle', 'missing', 'identity', 'active', 'budget', 'profile'])
def test_unsafe_source_capture_fails_closed(tmp_path, case):
    from api.history_capture import capture_sources
    home = sources(tmp_path)
    path = home / 'sessions/parent.json'
    data = json.loads(path.read_text())
    if case == 'cycle':
        data['parent_session_id'] = 'child'
    elif case == 'missing':
        data['parent_session_id'] = 'absent'
    elif case == 'identity':
        data['session_id'] = 'wrong'
    elif case == 'active':
        data['active_stream_id'] = 'running'
    elif case == 'profile':
        data['profile'] = 'foreign-profile'
    path.write_text(json.dumps(data))
    with pytest.raises((ValueError, FileNotFoundError)):
        capture_sources(home / 'sessions', home / 'state.db', 'child', profile_identity='test',
                        max_bytes=1 if case == 'budget' else 100000)


def test_differential_reports_real_page_failure_and_still_walks_all_pages(tmp_path, monkeypatch):
    from api.history_capture import capture_sources, compare_capture
    from api.display_history import HistoryStore
    home = sources(tmp_path)
    capture = capture_sources(home / 'sessions', home / 'state.db', 'child', profile_identity='test', max_bytes=100000)
    original = HistoryStore.page_shadow
    def corrupt(self, *args, **kwargs):
        page = original(self, *args, **kwargs)
        page['message_count'] += 1
        return page
    monkeypatch.setattr(HistoryStore, 'page_shadow', corrupt)
    report = compare_capture(capture, tmp_path / 'shadow.db', limits=[1], max_bytes=100000)
    assert report['status'] == 'FAIL'
    assert report['limits'][0]['complete']
    assert report['limits'][0]['pages'] == 3
    assert all(p['different_fields'] == ['message_count'] for p in report['limits'][0]['checks'])


def sources(tmp_path):
    home = tmp_path / 'source'
    home.mkdir()
    sessions = home / 'sessions'
    sessions.mkdir()
    (sessions / 'child.json').write_text(json.dumps({
        'session_id': 'child', 'profile': 'default', 'parent_session_id': 'parent',
        'messages': [{'role': 'user', 'content': 'child', 'timestamp': 3}],
    }))
    (sessions / 'parent.json').write_text(json.dumps({
        'session_id': 'parent', 'profile': 'default', 'pre_compression_snapshot': True,
        'messages': [{'role': 'user', 'content': 'parent', 'timestamp': 1}],
    }))
    with sqlite3.connect(home / 'state.db') as db:
        db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,timestamp REAL,active INTEGER)')
        db.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?)', [
            (1, 'child', 'assistant', 'archived', 2, 0),
            (2, 'child', 'assistant', 'answer', 4, 1),
        ])
    return home


def test_capture_binds_exact_disk_sources_without_writes(tmp_path):
    assert importlib.util.find_spec('api.history_capture'), 'read-only source capture missing'
    from api.history_capture import capture_sources, verify_capture, merged_messages
    home = sources(tmp_path)
    before = {str(p): p.read_bytes() for p in home.rglob('*') if p.is_file()}
    capture = capture_sources(home / 'sessions', home / 'state.db', 'child', profile_identity='test-profile', max_bytes=100000)
    verify_capture(capture)
    assert [m['content'] for m in merged_messages(capture)] == ['parent', 'child', 'answer']
    assert len(capture['manifest']['sources']) == 3
    assert capture['manifest']['routing'] == 'LEGACY'
    assert capture['manifest']['inactive_rows'] == 1
    assert before == {str(p): p.read_bytes() for p in home.rglob('*') if p.is_file()}
