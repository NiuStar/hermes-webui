"""Unwired necessary binding rejection checks, never a write capability.

ScopeKey must come from server authorization, not a client path. A matching
binding remains BLOCKED until source gates and business invariants are wired.
"""
from dataclasses import dataclass, field
import sqlite3


@dataclass(frozen=True)
class ScopeKey:
    profile_identity: str
    session_id: str


@dataclass(frozen=True)
class Binding:
    scope_id: str
    epoch: int
    actual_revision: int
    mutation_id: str
    run_id: str
    writer_token: str = field(repr=False)


def check_new_event_binding(db: sqlite3.Connection, scope: ScopeKey, binding: Binding) -> str:
    """Read-only preflight; REJECTED_BINDING or BLOCKED, never authorization."""
    if type(scope) is not ScopeKey or type(binding) is not Binding:
        return 'REJECTED_BINDING'
    identifiers = (scope.profile_identity, scope.session_id, binding.scope_id,
                   binding.mutation_id, binding.run_id, binding.writer_token)
    if any(type(value) is not str or not value for value in identifiers):
        return 'REJECTED_BINDING'
    try:
        for value in identifiers:
            value.encode('utf-8', errors='strict')
    except UnicodeEncodeError:
        return 'REJECTED_BINDING'
    if (type(binding.epoch) is not int or not 0 <= binding.epoch <= 9007199254740991
            or type(binding.actual_revision) is not int
            or not 1 <= binding.actual_revision <= 9007199254740991):
        return 'REJECTED_BINDING'
    try:
        if db.in_transaction or db.row_factory is not None:
            return 'BLOCKED'
        if (db.execute('PRAGMA foreign_keys').fetchone() != (1,)
                or db.execute('PRAGMA synchronous').fetchone() != (2,)
                or db.execute('PRAGMA journal_mode').fetchone() != ('wal',)):
            return 'BLOCKED'
        row = db.execute('''
            SELECT 1 FROM scopes s
            JOIN mutations m ON m.scope_id=s.scope_id
            JOIN runs r ON r.scope_id=m.scope_id AND r.mutation_id=m.mutation_id
            WHERE s.scope_id=? AND s.profile_identity=? AND s.session_id=?
              AND s.epoch=? AND s.revision=? AND m.mutation_id=? AND r.run_id=?
              AND m.epoch=s.epoch AND r.epoch=m.epoch
              AND m.actual_revision=s.revision AND r.actual_revision=m.actual_revision
              AND m.writer_token=? AND r.writer_token=m.writer_token
              AND m.state='PREPARED' AND m.recovery_only=0 AND r.state='OPEN'
            ''', (binding.scope_id, scope.profile_identity, scope.session_id,
                  binding.epoch, binding.actual_revision, binding.mutation_id,
                  binding.run_id, binding.writer_token)).fetchone()
    except sqlite3.Error:
        return 'BLOCKED'
    if row is None:
        return 'REJECTED_BINDING'
    return 'BLOCKED'

