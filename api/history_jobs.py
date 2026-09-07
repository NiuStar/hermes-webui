"""Independent shadow observation queue. Not the reviewed protocol schema.

No runtime discovery, source access, candidate construction or publication occurs
here. Explicit observations are caller assertions, not trusted terminal receipts.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import uuid

from api.history_capture import digest


class HistoryJobStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS shadow_job_observations(
                    scope TEXT NOT NULL, operation_key TEXT NOT NULL,
                    input_hash TEXT NOT NULL, input_json TEXT NOT NULL,
                    PRIMARY KEY(scope,operation_key));
                CREATE TABLE IF NOT EXISTS shadow_jobs(
                    scope TEXT NOT NULL, job_id TEXT NOT NULL,
                    operation_key TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK(state IN ('PENDING','LEASED','SHADOW_REPORTED')),
                    attempt INTEGER NOT NULL DEFAULT 0, fence INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(scope,job_id), UNIQUE(scope,operation_key),
                    FOREIGN KEY(scope,operation_key)
                        REFERENCES shadow_job_observations(scope,operation_key));
                CREATE TABLE IF NOT EXISTS shadow_job_attempts(
                    scope TEXT NOT NULL, job_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                    fence INTEGER NOT NULL, owner TEXT NOT NULL, lease_until INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('LEASED','EXPIRED','SHADOW_REPORTED')),
                    PRIMARY KEY(scope,job_id,attempt), UNIQUE(scope,job_id,fence),
                    FOREIGN KEY(scope,job_id) REFERENCES shadow_jobs(scope,job_id));
                CREATE TABLE IF NOT EXISTS shadow_job_reports(
                    scope TEXT NOT NULL, job_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                    report_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope,job_id),
                    FOREIGN KEY(scope,job_id,attempt)
                        REFERENCES shadow_job_attempts(scope,job_id,attempt));
            """)
            for table in ('shadow_job_observations', 'shadow_job_reports'):
                for operation in ('UPDATE', 'DELETE'):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{operation} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable'); END")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, scope, **observation):
        required = {'run_id', 'stream_id', 'terminal', 'operation_key',
                    'capture_identity', 'capture_sha256', 'source_manifest',
                    'algorithm_version', 'ephemeral'}
        if set(observation) != required or observation['ephemeral'] is not False:
            raise ValueError('explicit non-ephemeral observation required')
        if observation['terminal'] not in ('normal', 'cancelled', 'failed'):
            raise ValueError('explicit terminal required')
        for key in required - {'ephemeral', 'source_manifest'}:
            if not isinstance(observation[key], str) or not observation[key]:
                raise ValueError('explicit identity required')
        import re
        if not re.fullmatch('[0-9a-f]{64}', observation['capture_sha256']):
            raise ValueError('invalid capture hash')
        sources = observation['source_manifest']
        if not isinstance(sources, list) or not sources:
            raise ValueError('source manifest required')
        for source in sources:
            if (not isinstance(source, dict) or set(source) != {'kind', 'identity', 'sha256'}
                    or not all(isinstance(v, str) and v for v in source.values())
                    or not re.fullmatch('[0-9a-f]{64}', source['sha256'])):
                raise ValueError('invalid source manifest')
        encoded = json.dumps(observation, sort_keys=True, ensure_ascii=False, allow_nan=False)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT j.job_id, o.input_hash FROM shadow_jobs j
                JOIN shadow_job_observations o USING(scope,operation_key)
                WHERE scope=? AND operation_key=?''',
                             (scope.key, observation['operation_key'])).fetchone()
            if row:
                if row['input_hash'] != digest(observation):
                    raise ValueError('input conflict')
                return row['job_id']
            job_id = uuid.uuid4().hex
            db.execute('INSERT INTO shadow_job_observations VALUES(?,?,?,?)',
                       (scope.key, observation['operation_key'], digest(observation), encoded))
            db.execute('INSERT INTO shadow_jobs(scope,job_id,operation_key) VALUES(?,?,?)',
                       (scope.key, job_id, observation['operation_key']))
            return job_id

    def claim(self, scope, job_id, *, owner, now, lease_seconds):
        """Short transaction only; the caller must build outside this method.

        `now` is an explicit trusted scheduler clock, not a client timestamp.
        """
        if (not isinstance(owner, str) or not owner or type(now) is not int or now < 0
                or type(lease_seconds) is not int or lease_seconds <= 0
                or now + lease_seconds > 2**63 - 1):
            raise ValueError('invalid lease')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute('SELECT * FROM shadow_jobs WHERE scope=? AND job_id=?',
                             (scope.key, job_id)).fetchone()
            if not job or job['state'] == 'SHADOW_REPORTED':
                return None
            if job['state'] == 'LEASED':
                previous = db.execute('''SELECT * FROM shadow_job_attempts
                    WHERE scope=? AND job_id=? AND attempt=?''',
                    (scope.key, job_id, job['attempt'])).fetchone()
                if previous['lease_until'] > now:
                    return None
                db.execute('''UPDATE shadow_job_attempts SET state='EXPIRED'
                    WHERE scope=? AND job_id=? AND attempt=?''',
                    (scope.key, job_id, job['attempt']))
            attempt, fence = job['attempt'] + 1, job['fence'] + 1
            db.execute("UPDATE shadow_jobs SET state='LEASED',attempt=?,fence=? WHERE scope=? AND job_id=?",
                       (attempt, fence, scope.key, job_id))
            db.execute('INSERT INTO shadow_job_attempts VALUES(?,?,?,?,?,?,?)',
                       (scope.key, job_id, attempt, fence, owner, now + lease_seconds, 'LEASED'))
            return dict(scope=scope.key, job_id=job_id, attempt=attempt, fence=fence,
                        owner=owner, lease_until=now + lease_seconds)

    def report_shadow(self, scope, claim, *, now, report_sha256):
        """Record an unverified report, NEVER a generation seal or publication.

        Cross-database candidate atomicity is NOT_IMPLEMENTED. This receipt must
        not be consumed as proof a candidate exists, is valid, or is publishable.
        """
        import re
        if (type(now) is not int or not 0 <= now <= 2**63 - 1
                or not isinstance(report_sha256, str)
                or not re.fullmatch('[0-9a-f]{64}', report_sha256)):
            raise ValueError('invalid report')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT a.* FROM shadow_job_attempts a
                JOIN shadow_jobs j USING(scope,job_id,attempt,fence)
                WHERE a.scope=? AND a.job_id=? AND a.attempt=? AND a.fence=?
                AND a.owner=? AND a.lease_until=? AND a.lease_until>?
                AND a.state='LEASED' AND j.state='LEASED' ''',
                (scope.key, claim['job_id'], claim['attempt'], claim['fence'],
                 claim['owner'], claim['lease_until'], now)).fetchone()
            if claim['scope'] != scope.key or not row:
                raise ValueError('stale claim')
            db.execute('INSERT INTO shadow_job_reports VALUES(?,?,?,?)',
                       (scope.key, claim['job_id'], claim['attempt'], report_sha256))
            db.execute("UPDATE shadow_job_attempts SET state='SHADOW_REPORTED' WHERE scope=? AND job_id=? AND attempt=?",
                       (scope.key, claim['job_id'], claim['attempt']))
            db.execute("UPDATE shadow_jobs SET state='SHADOW_REPORTED' WHERE scope=? AND job_id=?",
                       (scope.key, claim['job_id']))

    def ready(self, scope, *, now):
        """Discover existing queue work, not missing runtime terminal observations."""
        with self._connect() as db:
            return [r['job_id'] for r in db.execute('''SELECT j.job_id FROM shadow_jobs j
                LEFT JOIN shadow_job_attempts a USING(scope,job_id,attempt,fence)
                WHERE j.scope=? AND (j.state='PENDING' OR
                    (j.state='LEASED' AND a.lease_until<=?)) ORDER BY j.job_id''',
                (scope.key, now))]

    def get(self, scope, job_id):
        with self._connect() as db:
            row = db.execute('''SELECT j.*, o.input_hash, o.input_json FROM shadow_jobs j
                JOIN shadow_job_observations o USING(scope,operation_key)
                WHERE j.scope=? AND j.job_id=?''', (scope.key, job_id)).fetchone()
            return dict(row) if row else None
