import json
from types import SimpleNamespace
from urllib.parse import urlparse

from api import request_diagnostics as rd


def test_nested_session_timing_is_inclusive_and_exclusive(monkeypatch):
    assert hasattr(rd, 'session_timed_call')
    monkeypatch.setenv('HERMES_WEBUI_SESSION_FINE_TIMING', '1')
    ticks = iter([0, 1, 2, 4, 7, 10])
    monkeypatch.setattr(rd.time, 'monotonic', lambda: next(ticks))
    logs = []
    handler = SimpleNamespace(headers={'X-WebUI-Timing-ID': 'test-1'}, _safe_webui_print=logs.append)

    @rd.session_timing_request
    def route(handler, parsed):
        return rd.session_timed_call('outer', lambda: rd.session_timed_call('inner', lambda: 42))

    assert route(handler, urlparse('/api/session')) == 42
    record = json.loads(logs[0].split(': ', 1)[1])
    spans = {s['name']: s for s in record['spans']}
    assert spans['outer']['inclusive_ms'] == 6000
    assert spans['outer']['exclusive_ms'] == 4000
    assert spans['inner']['inclusive_ms'] == 2000
    assert record['exclusive_sum_ms'] + record['unattributed_ms'] == record['elapsed_ms']
    assert record['request_id'] == 'test-1'


def test_disabled_and_other_routes_do_not_time(monkeypatch):
    import pytest
    for enabled, path, rid in [('0', '/api/session', 'ok'), ('1', '/health', 'ok'), ('1', '/api/session', 'bad\nsecret')]:
        monkeypatch.setenv('HERMES_WEBUI_SESSION_FINE_TIMING', enabled)
        monkeypatch.setattr(rd.time, 'monotonic', lambda: pytest.fail('disabled clock used'))
        handler = SimpleNamespace(headers={'X-WebUI-Timing-ID': rid}, _safe_webui_print=lambda _: pytest.fail('logged'))
        @rd.session_timing_request
        def route(handler, parsed):
            return rd.session_timed_call('identity', lambda x: x, 19)
        assert route(handler, urlparse(path)) == 19


def test_exception_cleanup_log_failure_and_span_bound(monkeypatch):
    import pytest
    monkeypatch.setenv('HERMES_WEBUI_SESSION_FINE_TIMING', '1')
    records = []
    handler = SimpleNamespace(headers={'X-WebUI-Timing-ID': 'error'}, _safe_webui_print=records.append)
    @rd.session_timing_request
    def route(handler, parsed):
        for _ in range(150):
            rd.session_timed_call('bounded', lambda: None)
        raise ValueError('private error text')
    with pytest.raises(ValueError, match='private error text'):
        route(handler, urlparse('/api/session'))
    assert rd._session_timing.get() is None
    assert len(json.loads(records[0].split(': ', 1)[1])['spans']) == 128
    assert 'private error text' not in records[0]
    handler._safe_webui_print = lambda _: (_ for _ in ()).throw(RuntimeError())
    with pytest.raises(ValueError):
        route(handler, urlparse('/api/session'))
