"""Offline source capture. Never call live Session.load/get_session (they can save).

The manifest binds observed objects, NOT a writer fence or a consistent multi-store
snapshot. Only the SQLite read transaction is atomic. Every scope stays LEGACY.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def capture_sources(session_dir, state_db, session_id, *, profile_identity, max_bytes):
    if not isinstance(profile_identity, str) or not profile_identity:
        raise ValueError('explicit profile identity required')
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError('explicit capture byte budget required')
    root, db_path = Path(session_dir).resolve(), Path(state_db).resolve(strict=True)
    objects, entries, observed = {}, [], {}
    remaining = max_bytes
    sid = session_id
    while sid:
        if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', sid):
            raise ValueError('unsafe session identity')
        if sid in objects or len(objects) >= 20:
            raise ValueError('incomplete or cyclic parent chain')
        path = root / (sid + '.json')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as source:
            stat = os.fstat(source.fileno())
            raw = source.read(remaining + 1)
            if len(raw) > remaining:
                raise ValueError('capture byte budget exceeded')
            remaining -= len(raw)
            after = os.fstat(source.fileno())
            if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('source changed during capture')
        obj = json.loads(raw)
        if obj.get('session_id') != sid:
            raise ValueError('sidecar identity mismatch')
        if obj.get('active_stream_id') or obj.get('pending_user_message'):
            raise ValueError('active session is not historical')
        objects[sid] = obj
        observed[path] = hashlib.sha256(raw).hexdigest()
        entries.append({'kind': 'sidecar', 'identity': str(path), 'session_id': sid,
                        'device': stat.st_dev, 'inode': stat.st_ino,
                        'raw_sha256': observed[path], 'object_sha256': digest(obj)})
        sid = obj.get('parent_session_id')
    if not objects:
        raise ValueError('session identity required')
    with sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        schema = [dict(r) for r in db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]
        columns = {r['name'] for r in db.execute('PRAGMA table_info(messages)')}
        if not {'id', 'session_id', 'role', 'content', 'timestamp'} <= columns:
            raise ValueError('unsupported messages schema')
        placeholders = ','.join('?' for _ in objects)
        rows = []
        for row in db.execute(f'SELECT * FROM messages WHERE session_id IN ({placeholders}) ORDER BY id', list(objects)):
            item = dict(row)
            remaining -= len(json.dumps(item, ensure_ascii=False).encode())
            if remaining < 0:
                raise ValueError('capture byte budget exceeded')
            rows.append(item)
        tables = {r['name'] for r in schema if r['type'] == 'table'}
        metadata = ([dict(r) for r in db.execute(f'SELECT * FROM sessions WHERE id IN ({placeholders}) ORDER BY id', list(objects))]
                    if 'sessions' in tables else [])
    for path, expected in observed.items():
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('source changed during capture')
    state = {'schema': schema, 'messages': rows, 'sessions': metadata}
    entries.append({'kind': 'state_db', 'identity': str(db_path),
                    'schema_sha256': digest(schema), 'object_sha256': digest(state),
                    'row_count': len(rows), 'coverage': 'all columns; requested sidecar ancestry; active and inactive'})
    capture = {'schema': 'history-capture-v1', 'scope': [profile_identity, session_id],
               'sidecars': objects, 'state_db': state,
               'manifest': {'routing': 'LEGACY', 'consistency': 'observed-only; no cross-store fence',
                            'sources': entries, 'inactive_rows': sum(r.get('active') == 0 for r in rows)}}
    capture['sha256'] = digest(capture)
    return capture


def verify_capture(capture):
    body = {k: v for k, v in capture.items() if k != 'sha256'}
    if capture.get('schema') != 'history-capture-v1' or digest(body) != capture.get('sha256'):
        raise ValueError('capture integrity mismatch')
    entries = capture['manifest']['sources']
    for entry in entries:
        obj = capture['state_db'] if entry['kind'] == 'state_db' else capture['sidecars'][entry['session_id']]
        if digest(obj) != entry['object_sha256']:
            raise ValueError('manifest object mismatch')


def compare_capture(capture, output_db, *, limits, max_bytes):
    """Walk every cursor to zero, reporting hashes/field names, never transcript text."""
    from api.display_history import HistoryStore, ScopeKey
    from api import routes
    verify_capture(capture)
    limits = list(dict.fromkeys(limits))
    if not limits or any(type(n) is not int or not 1 <= n <= 500 for n in limits):
        raise ValueError('limits must be nonempty integers in 1..500')
    messages = merged_messages(capture)
    session = _session(capture, capture['scope'][1])
    tools = session.tool_calls or []
    scenes = getattr(session, 'anchor_activity_scenes', None)
    scope = ScopeKey(*capture['scope'])
    store = HistoryStore(output_db)
    generation = store.build_shadow(scope, messages, tool_calls=tools, scenes=scenes,
                                    max_bytes=max_bytes, capture=capture)
    report = {'status': 'PASS', 'routing': 'LEGACY', 'capture_sha256': capture['sha256'],
              'generation': generation, 'message_count': len(messages), 'limits': [],
              'oracle': 'legacy limited-display merge/window/hydration; detached source objects, not live GET'}
    for limit in limits:
        before, pages = None, []
        while True:
            selected, offset = routes._message_window_for_display(messages, msg_limit=limit, msg_before=before)
            selected = routes._messages_for_limited_payload(selected)
            selected = routes._hydrate_anchor_activity_scenes(selected, scenes, message_offset=offset, tool_calls=tools)
            windowed = before is not None or len(selected) < len(messages)
            expected = {'messages': selected, 'message_count': len(messages),
                        'tool_calls': routes._tool_calls_for_message_window(tools, offset, len(selected)) if windowed else tools,
                        '_messages_offset': offset, '_messages_truncated': offset > 0}
            actual = store.page_shadow(scope, generation, limit=limit, before=before)
            differences = [k for k in expected if actual[k] != expected[k]]
            pages.append({'before': before, 'offset': offset, 'rows': len(selected),
                          'expected_sha256': digest(expected), 'actual_sha256': digest(actual),
                          'different_fields': differences})
            if differences:
                report['status'] = 'FAIL'
            if offset == 0:
                break
            if before is not None and offset >= before:
                raise ValueError('non-progressing legacy cursor')
            before = offset
        report['limits'].append({'limit': limit, 'pages': len(pages), 'complete': True, 'checks': pages})
    return report


def _session(capture, sid):
    from api.models import Session, _collapse_adjacent_duplicate_partials
    data = copy.deepcopy(capture['sidecars'][sid])
    data['messages'], _ = _collapse_adjacent_duplicate_partials(data.get('messages'))
    return Session(**data)


def _state_messages(capture, sid):
    from api.models import _project_state_db_message
    rows = capture['state_db']['messages']
    optional = ['tool_call_id', 'tool_calls', 'tool_name', 'reasoning', 'reasoning_details',
                'codex_reasoning_items', 'reasoning_content', 'codex_message_items', 'api_content']
    return [_project_state_db_message(r, set(r), True, optional) for r in rows
            if r['session_id'] == sid and r.get('active') != 0]


def merged_messages(capture):
    """Legacy limited-display merge on detached objects; no runtime recovery I/O."""
    from unittest.mock import patch
    from api import routes
    verify_capture(capture)
    sid = capture['scope'][1]
    session = _session(capture, sid)
    # Reject unsupported foreign/CLI stitch instead of silently dropping DB parents.
    if routes._is_messaging_session_record(session) or getattr(session, 'is_cli_session', False):
        raise ValueError('foreign/CLI capture requires a source-specific adapter')
    def load(parent_id, **kwargs):
        return _session(capture, parent_id)
    with patch.object(routes.Session, 'load', side_effect=load), \
         patch.object(routes, 'get_session', side_effect=load), \
         patch.object(routes, '_display_merge_session_is_active', return_value=True), \
         patch.object(routes, '_sidecar_stat_signature', return_value=None, create=True):
        sidecar = routes._webui_sidecar_lineage_messages_for_display(session)
        return routes._limited_webui_messages_for_display_with_sidecar(
            session, sidecar, _state_messages(capture, sid), msg_before=0)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    cap = sub.add_parser('capture')
    for name in ('session-dir', 'state-db', 'session-id', 'profile-identity'):
        cap.add_argument('--' + name, required=True)
    comp = sub.add_parser('compare')
    comp.add_argument('--capture', required=True)
    comp.add_argument('--output-db', required=True)
    comp.add_argument('--limits', required=True)
    for command in (cap, comp):
        command.add_argument('--max-bytes', type=int, required=True)
        command.add_argument('--output', required=True, help='new private artifact; never overwrites')
    args = parser.parse_args()
    # Compare imports runtime modules only under an explicitly isolated home.
    if args.command == 'compare' and (not os.environ.get('HERMES_HOME') or not os.environ.get('HERMES_WEBUI_STATE_DIR')):
        parser.error('compare requires isolated HERMES_HOME and HERMES_WEBUI_STATE_DIR')
    if Path(args.output).exists():
        parser.error('output already exists')
    try:
        if args.command == 'capture':
            result = capture_sources(args.session_dir, args.state_db, args.session_id,
                                     profile_identity=args.profile_identity, max_bytes=args.max_bytes)
        else:
            if Path(args.output_db).exists():
                raise ValueError('output database already exists')
            result = compare_capture(json.loads(Path(args.capture).read_text()), args.output_db,
                                     limits=[int(n) for n in args.limits.split(',')], max_bytes=args.max_bytes)
        with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
            json.dump(result, output, ensure_ascii=False, allow_nan=False)
        print(json.dumps({'status': result.get('status', 'CAPTURED'), 'routing': 'LEGACY',
                          'sha256': result.get('capture_sha256', result.get('sha256'))}))
        return 0 if result.get('status', 'PASS') == 'PASS' else 1
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        # Do not leak paths, DB rows, or transcript via exception repr.
        print(json.dumps({'status': 'BLOCKED', 'routing': 'LEGACY', 'error_type': type(exc).__name__}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
