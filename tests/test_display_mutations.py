"""Synthetic persisted protocol fixtures, never production eligibility proof."""
import importlib
from dataclasses import replace
import sqlite3

import pytest

from pathlib import Path
import re


def initialize_reference_fixture(connection):
    """Synthetic reference-DDL fixture, never bootstrap/activation evidence."""
    document = (Path(__file__).parents[1] /
                'docs/session-read-write-separation-coding-schema.md').read_text()
    blocks = re.findall(r'```sql\n(.*?)```', document, re.S)
    if not blocks or any(not block.strip() for block in blocks):
        pytest.fail('approved reference DDL is missing or empty')
    connection.executescript('\n'.join(blocks))
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA synchronous=FULL')
    assert connection.execute('PRAGMA journal_mode=WAL').fetchone() == ('wal',)
    assert connection.execute('PRAGMA foreign_keys').fetchone() == (1,)
    assert connection.execute('PRAGMA synchronous').fetchone() == (2,)


def module():
    try:
        return importlib.import_module('api.display_mutations')
    except ModuleNotFoundError:
        pytest.fail('formal binding rejection gate is missing')


@pytest.fixture
def db(tmp_path, request):
    connection = sqlite3.connect(tmp_path / 'display.db')
    # Register before initialization so setup failures release the connection.
    # SQLite's connection context manager alone does not close the handle.
    request.addfinalizer(connection.close)
    initialize_reference_fixture(connection)
    for scope, profile in [('s1', 'p1'), ('s2', 'p2')]:
        connection.execute('INSERT INTO scopes(scope_id,profile_identity,session_id) VALUES(?,?,?)',
                           (scope, profile, 'same-session'))
        connection.execute('UPDATE scopes SET revision=1 WHERE scope_id=?', (scope,))
        connection.execute('''INSERT INTO mutations
            (scope_id,mutation_id,operation_key,input_hash,epoch,actual_revision,writer_token,kind,state,created_at)
            VALUES(?, 'm', 'op', 'hash', 0, 1, 'token', 'APPEND', 'PREPARED', 0)''', (scope,))
        connection.execute('''INSERT INTO runs
            (scope_id,run_id,mutation_id,epoch,actual_revision,writer_token,state,base_covered_seq)
            VALUES(?, 'r', 'm', 0, 1, 'token', 'OPEN', 0)''', (scope,))
    connection.commit()
    return connection


def test_cross_profile_binding_is_rejected_without_writes(db):
    api = module()
    before = db.total_changes
    result = api.check_new_event_binding(
        db, api.ScopeKey('p2', 'same-session'), api.Binding('s1', 0, 1, 'm', 'r', 'token'))
    assert result == 'REJECTED_BINDING'
    assert db.total_changes == before
    assert not db.in_transaction



@pytest.mark.parametrize('field,value', [
    ('scope_id', 'missing'), ('epoch', 1), ('actual_revision', 2),
    ('mutation_id', 'other'), ('run_id', 'other'), ('writer_token', 'other'),
    ('epoch', False), ('actual_revision', True), ('epoch', 0.0),
    ('scope_id', ''), ('writer_token', None), ('epoch', -1),
    ('actual_revision', 9007199254740992),
])
def test_every_binding_component_is_exact(db, field, value):
    api = module()
    binding = replace(api.Binding('s1', 0, 1, 'm', 'r', 'token'), **{field: value})
    assert api.check_new_event_binding(db, api.ScopeKey('p1', 'same-session'), binding) == 'REJECTED_BINDING'


@pytest.mark.parametrize('statement', [
    "UPDATE mutations SET state='UNCERTAIN' WHERE scope_id='s1'",
    "UPDATE mutations SET recovery_only=1,recovery_key='key',recovery_evidence_json='{}' WHERE scope_id='s1'",
    "UPDATE runs SET state='TERMINAL_PENDING' WHERE scope_id='s1'",
    "UPDATE scopes SET revision=2 WHERE scope_id='s1'",
    "UPDATE scopes SET epoch=1 WHERE scope_id='s1'",
])
def test_persisted_revocation_is_rejected(db, statement):
    api = module()
    db.execute(statement)
    db.commit()
    assert api.check_new_event_binding(db, api.ScopeKey('p1', 'same-session'),
        api.Binding('s1', 0, 1, 'm', 'r', 'token')) == 'REJECTED_BINDING'


@pytest.mark.parametrize('eligibility', ['LEGACY', 'ELIGIBLE'])
def test_exact_match_cannot_authorize_external_writes(db, eligibility):
    api = module()
    db.execute('UPDATE scopes SET eligibility=?', (eligibility,))
    db.commit()
    before = db.total_changes
    binding = api.Binding('s1', 0, 1, 'm', 'r', 'token')
    assert api.check_new_event_binding(db, api.ScopeKey('p1', 'same-session'), binding) == 'BLOCKED'
    assert db.total_changes == before
    assert 'token' not in repr(binding)


@pytest.mark.parametrize('field', [
    'profile_identity', 'session_id', 'scope_id', 'mutation_id', 'run_id', 'writer_token',
])
@pytest.mark.parametrize('value', ['\ud800', '\udfff', 'prefix\ud800suffix'])
def test_non_utf8_identifier_is_rejected_before_sql(db, field, value):
    api = module()
    scope = api.ScopeKey('p1', 'same-session')
    binding = api.Binding('s1', 0, 1, 'm', 'r', 'token')
    if field in ('profile_identity', 'session_id'):
        scope = replace(scope, **{field: value})
    else:
        binding = replace(binding, **{field: value})
    before = db.total_changes
    statements = []
    db.set_trace_callback(statements.append)
    try:
        assert api.check_new_event_binding(db, scope, binding) == 'REJECTED_BINDING'
        assert statements == []
        assert db.total_changes == before
        assert not db.in_transaction
    finally:
        db.set_trace_callback(None)


def test_second_connection_revocation_is_not_cached(db):
    api = module()
    scope = api.ScopeKey('p1', 'same-session')
    binding = api.Binding('s1', 0, 1, 'm', 'r', 'token')
    assert api.check_new_event_binding(db, scope, binding) == 'BLOCKED'
    other = sqlite3.connect(db.execute('PRAGMA database_list').fetchone()[2])
    try:
        other.execute("UPDATE mutations SET state='UNCERTAIN' WHERE scope_id='s1'")
        other.commit()
    finally:
        other.close()
    assert api.check_new_event_binding(db, scope, binding) == 'REJECTED_BINDING'



@pytest.mark.parametrize('mode', ['transaction', 'foreign_keys', 'synchronous', 'closed', 'row_factory'])
def test_unusable_connection_blocks_without_transaction_ownership(db, mode):
    api = module()
    # Deliberately invalid binding: connection failure takes priority, not a
    # potentially stale database-derived rejection.
    if mode == 'transaction':
        db.execute('BEGIN')
    elif mode == 'foreign_keys':
        db.execute('PRAGMA foreign_keys=OFF')
    elif mode == 'synchronous':
        db.execute('PRAGMA synchronous=NORMAL')
    elif mode == 'row_factory':
        db.row_factory = sqlite3.Row
    else:
        db.close()
    assert api.check_new_event_binding(db, api.ScopeKey('p1', 'same-session'),
        api.Binding('s1', 0, 1, 'm', 'r', 'wrong')) == 'BLOCKED'
    if mode == 'transaction':
        assert db.in_transaction
        db.rollback()
