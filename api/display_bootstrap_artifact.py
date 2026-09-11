"""Frozen private-database verification; never grants creation or activation."""
import hashlib
import os
import sqlite3
import stat
import time
import math

from api import _display_schema_ddl

PINNED_DDL_SHA = '578f80789de98456324d430142631c7b7f984f80f3ffe30fc7c31d0634bdcd9d'
CATALOG = "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"


def _hash(fd, deadline):
    os.lseek(fd, 0, os.SEEK_SET)
    value = hashlib.sha256()
    while True:
        if time.monotonic() >= deadline:
            raise ValueError('RESOURCE_LIMIT')
        block = os.read(fd, 65536)
        if time.monotonic() >= deadline:
            raise ValueError('RESOURCE_LIMIT')
        if not block:
            break
        value.update(block)
    return value.hexdigest()


def _identity(value):
    return {key: getattr(value, 'st_' + key) for key in ('dev','ino','uid','gid','mode','nlink')}


def build_private_database(directory_fd, *, max_bytes, deadline, before_growth=None):
    """Build only in an empty, caller-held private directory; retain failures."""
    if (type(max_bytes) is not int or max_bytes < 1
            or type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise ValueError('INVALID_INPUT')
    if time.monotonic() >= deadline or max_bytes < 4096:
        raise ValueError('RESOURCE_LIMIT')
    sql = _display_schema_ddl.SQL
    if hashlib.sha256(sql.encode('utf-8')).hexdigest() != PINNED_DDL_SHA:
        raise ValueError('UNKNOWN_SCHEMA')
    if os.listdir(directory_fd):
        raise ValueError('TARGET_CONFLICT')
    if before_growth is not None:
        before_growth()
    fd = os.open('display.sqlite', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory_fd)
    connection = None
    try:
        identity = _identity(os.fstat(fd))
        os.fsync(directory_fd)
        connection = sqlite3.connect(
            f'file:/proc/self/fd/{directory_fd}/display.sqlite?mode=rw&vfs=unix', uri=True)
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('PRAGMA temp_store=MEMORY')
        page_size = connection.execute('PRAGMA page_size').fetchone()[0]
        connection.execute(f'PRAGMA max_page_count={max_bytes // page_size}')
        if before_growth is not None:
            before_growth()
        if connection.execute('PRAGMA journal_mode=WAL').fetchone() != ('wal',):
            raise ValueError('UNSUPPORTED_PLATFORM')
        if before_growth is not None:
            before_growth()
        connection.executescript('BEGIN IMMEDIATE;\n' + sql + '\nCOMMIT;')
        if before_growth is not None:
            before_growth()
        checkpoint = connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if checkpoint != (0, 0, 0):
            raise ValueError('SIDECAR_REMAINS')
        connection.close()
        connection = None
        if _identity(os.stat('display.sqlite', dir_fd=directory_fd, follow_symlinks=False)) != identity:
            raise ValueError('IDENTITY_CHANGED')
        os.fsync(fd)
        os.fsync(directory_fd)
        return verify_frozen_database(directory_fd, max_bytes=max_bytes, deadline=deadline)
    except sqlite3.Error as exc:
        if (getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT
                and time.monotonic() >= deadline):
            raise ValueError('RESOURCE_LIMIT') from exc
        raise
    finally:
        if connection is not None:
            connection.close()
        os.close(fd)


def verify_frozen_database(directory_fd, *, max_bytes, deadline):
    """Verify only DB bytes; manifest and OS hard limits remain caller duties."""
    if type(max_bytes) is not int or max_bytes < 1 or type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError("INVALID_INPUT")
    if time.monotonic() >= deadline:
        raise ValueError("RESOURCE_LIMIT")
    sql = _display_schema_ddl.SQL
    if hashlib.sha256(sql.encode('utf-8')).hexdigest() != PINNED_DDL_SHA:
        raise ValueError('UNKNOWN_SCHEMA')
    entries = set(os.listdir(directory_fd))
    if any(name.endswith(('-wal', '-shm', '-journal')) for name in entries):
        raise ValueError('SIDECAR_REMAINS')
    if entries not in ({'display.sqlite'}, {'display.sqlite', 'manifest.json'}):
        raise ValueError('UNKNOWN_SCHEMA')
    fd = os.open('display.sqlite', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    connection = reference = None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError('IDENTITY_CHANGED')
        if max(before.st_size, before.st_blocks * 512) > max_bytes:
            raise ValueError("RESOURCE_LIMIT")
        before_hash = _hash(fd, deadline)
        # Held descriptor selects bytes; immutable is safe only inside the private
        # offline lifecycle, after all writable connections have been closed.
        connection = sqlite3.connect(f'file:/proc/self/fd/{fd}?mode=ro&immutable=1', uri=True)
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('BEGIN')
        reference = sqlite3.connect(':memory:')
        reference.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
        reference.executescript(sql)
        catalog = connection.execute(CATALOG).fetchall()
        if catalog != reference.execute(CATALOG).fetchall():
            raise ValueError('UNKNOWN_SCHEMA')
        if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('UNKNOWN_SCHEMA')
        if connection.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('UNKNOWN_SCHEMA')
        for kind, name, _, _ in catalog:
            if kind == 'table':
                quoted = '"' + name.replace('"', '""') + '"'
                if connection.execute('SELECT 1 FROM ' + quoted + ' LIMIT 1').fetchone():
                    raise ValueError('UNKNOWN_SCHEMA')
        connection.close()
        connection = None
        named = os.stat('display.sqlite', dir_fd=directory_fd, follow_symlinks=False)
        after = os.fstat(fd)
        if (_identity(before) != _identity(named) or _identity(before) != _identity(after)
                or before.st_size != after.st_size or _hash(fd, deadline) != before_hash
                or set(os.listdir(directory_fd)) != entries):
            raise ValueError('IDENTITY_CHANGED')
        if time.monotonic() >= deadline:
            raise ValueError('RESOURCE_LIMIT')
        return dict(db_sha=before_hash, db_bytes=before.st_size, db_identity=_identity(before),
                    verification=dict(integrity_check='ok', foreign_key_violations=0, empty_state=True))
    except sqlite3.Error as exc:
        if (getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT
                and time.monotonic() >= deadline):
            raise ValueError('RESOURCE_LIMIT') from exc
        raise
    finally:
        if connection is not None:
            connection.close()
        if reference is not None:
            reference.close()
        os.close(fd)
