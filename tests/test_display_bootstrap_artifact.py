"""Private artifact primitives, not an authorization or creation API."""
import hashlib
import os
import re
import sqlite3
from pathlib import Path
import pytest


def reference_sql():
    text = (Path(__file__).parents[1]/'docs/session-read-write-separation-coding-schema.md').read_text()
    return '\n'.join(re.findall(r'```sql\n(.*?)```',text,re.S))


def test_pinned_ddl_refuses_before_sqlite(tmp_path, monkeypatch):
    from api import display_bootstrap_artifact as artifact
    monkeypatch.setattr(artifact._display_schema_ddl, 'SQL', 'CREATE TABLE changed(x);')
    def forbidden(*args, **kwargs):
        pytest.fail('DDL mismatch must not open SQLite')
    monkeypatch.setattr(artifact.sqlite3, 'connect', forbidden)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError, match='UNKNOWN_SCHEMA'):
            artifact.verify_frozen_database(fd, max_bytes=1048576, deadline=__import__('time').monotonic()+10)
        assert list(tmp_path.iterdir()) == []
    finally:
        os.close(fd)


def test_verification_error_releases_descriptors(tmp_path):
    from api import display_bootstrap_artifact as artifact
    (tmp_path/'display.sqlite').write_bytes(b'invalid database')
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        before = set(os.listdir('/proc/self/fd'))
        for _ in range(3):
            with pytest.raises(sqlite3.DatabaseError):
                artifact.verify_frozen_database(fd, max_bytes=1048576, deadline=__import__('time').monotonic()+10)
        assert set(os.listdir('/proc/self/fd')) == before
        assert (tmp_path/'display.sqlite').read_bytes() == b'invalid database'
    finally:
        os.close(fd)


@pytest.mark.parametrize('budget,expired', [(1,False),(1048576,True)])
def test_resource_limit_preserves_file(tmp_path,budget,expired):
    import time
    from api import display_bootstrap_artifact as artifact
    db=sqlite3.connect(tmp_path/'display.sqlite')
    db.executescript(reference_sql())
    db.close()
    before=(tmp_path/'display.sqlite').read_bytes()
    fd=os.open(tmp_path,os.O_RDONLY|os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError,match='RESOURCE_LIMIT'):
            artifact.verify_frozen_database(fd,max_bytes=budget,deadline=time.monotonic()+(-1 if expired else 10))
        assert (tmp_path/'display.sqlite').read_bytes()==before
    finally:
        os.close(fd)


def test_exact_empty_artifact(tmp_path):
    from api import display_bootstrap_artifact as artifact
    db=sqlite3.connect(tmp_path/'display.sqlite')
    db.executescript(reference_sql())
    db.close()
    fd=os.open(tmp_path,os.O_RDONLY|os.O_DIRECTORY)
    try:
        before=hashlib.sha256((tmp_path/'display.sqlite').read_bytes()).hexdigest()
        result=artifact.verify_frozen_database(fd, max_bytes=1048576, deadline=__import__('time').monotonic()+10)
        assert result['db_sha']==before
        assert hashlib.sha256((tmp_path/'display.sqlite').read_bytes()).hexdigest()==before
        assert result['verification']==dict(integrity_check='ok',foreign_key_violations=0,empty_state=True)
        assert set(p.name for p in tmp_path.iterdir())=={'display.sqlite'}
    finally:
        os.close(fd)


@pytest.mark.parametrize('variant',['extra_table','missing_trigger','row','sidecar','hardlink'])
def test_reject_changed_artifact(tmp_path,variant):
    from api import display_bootstrap_artifact as artifact
    db=sqlite3.connect(tmp_path/'display.sqlite')
    db.executescript(reference_sql())
    if variant=='extra_table':
        db.execute('CREATE TABLE unexpected(x)')
    elif variant=='missing_trigger':
        db.execute('DROP TRIGGER events_binding')
    elif variant=='row':
        db.execute("INSERT INTO scopes(scope_id,profile_identity,session_id) VALUES('s','p','x')")
        db.commit()
    db.close()
    if variant=='sidecar':
        (tmp_path/'display.sqlite-wal').write_bytes(b'retain')
    if variant=='hardlink':
        os.link(tmp_path/'display.sqlite',tmp_path/'retained-link')
    fd=os.open(tmp_path,os.O_RDONLY|os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError):
            artifact.verify_frozen_database(fd, max_bytes=1048576, deadline=__import__('time').monotonic()+10)
    finally:
        os.close(fd)
