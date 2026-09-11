"""Retired initializer contract; reference fixtures confer no OS admission.

Original initializer race/rollback tests remain in Git and retained RED evidence.
The algorithm was retired, not repaired. Real creation is tested by lifecycle.
"""
import re
import sqlite3
from pathlib import Path

import pytest

from api.display_schema import initialize, LegacyInitializerDisabled


def reference_sql():
    document = (Path(__file__).parents[1] / 'docs/session-read-write-separation-coding-schema.md').read_text()
    blocks = re.findall(r'```sql\n(.*?)```', document, re.S)
    if not blocks or any(not block.strip() for block in blocks):
        pytest.fail('approved reference DDL is missing or empty')
    return '\n'.join(blocks)


def test_legacy_rejects_before_any_connection_access():
    class ForbiddenConnection:
        def __getattribute__(self, name):
            raise AssertionError('legacy initializer accessed connection')
    with pytest.raises(LegacyInitializerDisabled) as caught:
        initialize(ForbiddenConnection())
    assert caught.value.code == 'LEGACY_INITIALIZER_DISABLED'


@pytest.mark.parametrize('mode', ['foreign', 'missing_trigger', 'extra_table',
                                  'in_memory', 'transaction', 'attached_memory', 'attached_file', 'empty'])
def test_legacy_rejection_preserves_database_and_settings(tmp_path, mode):
    db = sqlite3.connect(':memory:' if mode == 'in_memory' else tmp_path / 'reject.sqlite')
    try:
        if mode == 'foreign':
            db.execute('CREATE TABLE important(value TEXT)')
            db.execute("INSERT INTO important VALUES ('preserve')")
            db.commit()
        elif mode in ('missing_trigger', 'extra_table'):
            db.executescript(reference_sql())  # synthetic reference, not admission
            db.execute('DROP TRIGGER events_binding' if mode == 'missing_trigger' else 'CREATE TABLE extra(value)')
        elif mode == 'transaction':
            db.execute('BEGIN IMMEDIATE')
        elif mode.startswith('attached_'):
            db.execute('ATTACH DATABASE ? AS other', (':memory:' if mode == 'attached_memory' else str(tmp_path/'attached.sqlite'),))
        catalog = 'SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY name'
        before = db.execute(catalog).fetchall()
        settings = lambda: [db.execute('PRAGMA ' + n).fetchall() for n in
                            ('database_list', 'foreign_keys', 'synchronous', 'journal_mode')]
        original_settings, transaction = settings(), db.in_transaction
        statements = []
        db.set_trace_callback(statements.append)
        with pytest.raises(LegacyInitializerDisabled, match='LEGACY_INITIALIZER_DISABLED'):
            initialize(db)
        db.set_trace_callback(None)
        assert statements == []
        assert db.in_transaction == transaction
        assert settings() == original_settings
        assert db.execute(catalog).fetchall() == before
        if mode == 'foreign':
            assert db.execute('SELECT * FROM important').fetchall() == [('preserve',)]
    finally:
        db.close()


def test_offline_builder_matches_independent_approved_ddl(tmp_path):
    """Real SQLite primitive; full OS admission is a separate integration gate."""
    import os
    import time
    from api.display_bootstrap_artifact import build_private_database
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        build_private_database(fd, max_bytes=1048576, deadline=time.monotonic()+10)
    finally:
        os.close(fd)
    catalog = 'SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY name'
    from contextlib import closing
    with closing(sqlite3.connect(':memory:')) as reference:
        reference.executescript(reference_sql())
        expected = reference.execute(catalog).fetchall()
    db = sqlite3.connect((tmp_path/'display.sqlite').as_uri()+'?mode=ro&immutable=1', uri=True)
    try:
        assert db.execute(catalog).fetchall() == expected
        assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    finally:
        db.close()
