"""Shadow-only durable task state machine; all databases are temporary."""
import importlib.util
import json
import multiprocessing
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import sys

import pytest

from api.display_history import ScopeKey
from api.history_capture import digest


def observation():
    return dict(run_id='run', stream_id='stream', terminal='normal',
                operation_key='terminal-observation', capture_identity='offline:capture-1',
                capture_sha256=digest({'fixture': 1}),
                source_manifest=[{'kind': 'sidecar', 'identity': 'offline:session',
                                  'sha256': digest({'source': 1})}],
                algorithm_version='shadow-v1', ephemeral=False)


def store_at(tmp_path):
    assert importlib.util.find_spec('api.history_jobs'), 'durable task store missing'
    from api.history_jobs import HistoryJobStore
    return HistoryJobStore(tmp_path / 'jobs.sqlite')


def test_atomic_enqueue_rolls_back_observation_on_sql_fault(tmp_path):
    store = store_at(tmp_path)
    with sqlite3.connect(store.path) as db:
        db.execute("CREATE TRIGGER fail_job BEFORE INSERT ON shadow_jobs BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        store.enqueue(ScopeKey('profile', 'session'), **observation())
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM shadow_job_observations').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM shadow_jobs').fetchone()[0] == 0


def test_atomic_claim_rolls_back_fence_on_sql_fault(tmp_path):
    store = store_at(tmp_path)
    scope = ScopeKey('profile', 'session')
    job = store.enqueue(scope, **observation())
    with sqlite3.connect(store.path) as db:
        db.execute("CREATE TRIGGER fail_attempt BEFORE INSERT ON shadow_job_attempts BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        store.claim(scope, job, owner='worker', now=1, lease_seconds=10)
    assert store.get(scope, job)['attempt'] == 0
    assert store.get(scope, job)['state'] == 'PENDING'


def test_atomic_report_rolls_back_receipt_on_sql_fault(tmp_path):
    store = store_at(tmp_path)
    scope = ScopeKey('profile', 'session')
    job = store.enqueue(scope, **observation())
    claim = store.claim(scope, job, owner='worker', now=1, lease_seconds=10)
    with sqlite3.connect(store.path) as db:
        db.execute("CREATE TRIGGER fail_report BEFORE UPDATE ON shadow_jobs WHEN NEW.state='SHADOW_REPORTED' BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        store.report_shadow(scope, claim, now=2, report_sha256=digest('report'))
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM shadow_job_reports').fetchone()[0] == 0
        assert db.execute('SELECT state FROM shadow_job_attempts').fetchone()[0] == 'LEASED'
    assert store.get(scope, job)['state'] == 'LEASED'


def test_concurrent_workers_only_one_claims_and_same_enqueue_reuses(tmp_path):
    store = store_at(tmp_path)
    scope = ScopeKey('profile', 'session')
    barrier = threading.Barrier(2)
    def enqueue(_):
        barrier.wait(timeout=5)
        return store.enqueue(scope, **observation())
    with ThreadPoolExecutor(2) as pool:
        jobs = list(pool.map(enqueue, range(2)))
    assert jobs[0] == jobs[1]
    def claim(owner):
        barrier.wait(timeout=5)
        return store.claim(scope, jobs[0], owner=owner, now=1, lease_seconds=10)
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(claim, ['one', 'two']))
    assert sum(c is not None for c in claims) == 1


def _child_claim(path, result):
    from api.history_jobs import HistoryJobStore
    store = HistoryJobStore(path)
    scope = ScopeKey('profile', 'session')
    job = store.enqueue(scope, **observation())
    result.put(store.claim(scope, job, owner='child', now=1, lease_seconds=10))


def test_fresh_process_exit_then_restart_recovers_without_tools(tmp_path, monkeypatch):
    from api.history_jobs import HistoryJobStore
    ctx = multiprocessing.get_context('spawn')
    result = ctx.Queue()
    path = tmp_path / 'jobs.sqlite'
    process = ctx.Process(target=_child_claim, args=(path, result))
    # spawn resolves the pickled target in a fresh interpreter. The shared
    # fixture restores an agent-first sys.path after earlier tests, where the
    # agent's own `tests` package hides this one. Pin only the spawn snapshot;
    # never change the shared fixture or the caller's lasting import order.
    try:
        with monkeypatch.context() as child_imports:
            child_imports.syspath_prepend(str(Path(__file__).resolve().parents[1]))
            process.start()
        # This single small claim fits in the queue pipe. Check process failure
        # before receiving so an import crash reports its exit code and stderr,
        # rather than hiding behind Queue.Empty twenty seconds later.
        process.join(timeout=20)
        assert process.exitcode == 0, f'claim child exited with {process.exitcode}'
        first = result.get(timeout=20)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=20)
        if process.pid is not None:
            process.close()
        result.close()
        result.join_thread()
    restarted = HistoryJobStore(path)
    scope = ScopeKey('profile', 'session')
    assert restarted.ready(scope, now=11) == [first['job_id']]
    second = restarted.claim(scope, first['job_id'], owner='restart', now=11, lease_seconds=10)
    assert second['fence'] == 2
    assert json.loads(restarted.get(scope, first['job_id'])['input_json']) == observation()


def test_restart_with_shadowing_tests_package_preserves_parent_path(tmp_path, monkeypatch):
    shadow = tmp_path / 'other-checkout'
    package = shadow / 'tests'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('', encoding='utf-8')
    monkeypatch.syspath_prepend(str(shadow))
    before = list(sys.path)
    children_before = {p.pid for p in multiprocessing.active_children()}
    test_fresh_process_exit_then_restart_recovers_without_tools(tmp_path, monkeypatch)
    assert sys.path == before
    assert {p.pid for p in multiprocessing.active_children()} == children_before


@pytest.mark.parametrize('terminal', ['normal', 'cancelled', 'failed'])
def test_all_terminal_outcomes_are_explicit_legal_inputs(tmp_path, terminal):
    store = store_at(tmp_path)
    scope = ScopeKey('profile', 'session')
    job = store.enqueue(scope, **dict(observation(), terminal=terminal))
    assert json.loads(store.get(scope, job)['input_json'])['terminal'] == terminal


def test_observation_and_report_are_immutable(tmp_path):
    store = store_at(tmp_path)
    scope = ScopeKey('profile', 'session')
    job = store.enqueue(scope, **observation())
    claim = store.claim(scope, job, owner='worker', now=1, lease_seconds=10)
    store.report_shadow(scope, claim, now=2, report_sha256=digest('report'))
    with sqlite3.connect(store.path) as db:
        for table in ('shadow_job_observations', 'shadow_job_reports'):
            with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                db.execute(f'DELETE FROM {table}')
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            db.execute("UPDATE shadow_job_observations SET input_json='{}'")


def test_restart_discovers_only_its_persisted_ready_jobs(tmp_path):
    store = store_at(tmp_path)
    scope = ScopeKey('profile', 'session')
    pending = store.enqueue(scope, **observation())
    leased = store.enqueue(scope, **dict(observation(), operation_key='second', run_id='two'))
    store.claim(scope, leased, owner='worker', now=100, lease_seconds=10)
    other = ScopeKey('other', 'session')
    store.enqueue(other, **observation())
    restarted = store_at(tmp_path)
    assert hasattr(restarted, 'ready'), 'persistent recovery enumeration missing'
    assert restarted.ready(scope, now=109) == [pending]
    assert set(restarted.ready(scope, now=110)) == {pending, leased}
    assert restarted.get(other, pending) is None
    assert restarted.claim(other, pending, owner='worker', now=110, lease_seconds=10) is None


@pytest.mark.parametrize('args', [dict(owner='', now=1, lease_seconds=1),
    dict(owner='worker', now=-1, lease_seconds=1),
    dict(owner='worker', now=True, lease_seconds=1),
    dict(owner='worker', now=1, lease_seconds=0),
    dict(owner='worker', now=1, lease_seconds=float('inf'))])
def test_invalid_lease_rejected(tmp_path, args):
    store = store_at(tmp_path)
    scope = ScopeKey('profile', 'session')
    job = store.enqueue(scope, **observation())
    with pytest.raises(ValueError):
        store.claim(scope, job, **args)
    assert store.get(scope, job)['state'] == 'PENDING'


def test_invalid_report_hash_rejected(tmp_path):
    store = store_at(tmp_path)
    scope = ScopeKey('profile', 'session')
    job = store.enqueue(scope, **observation())
    claim = store.claim(scope, job, owner='worker', now=1, lease_seconds=10)
    with pytest.raises(ValueError):
        store.report_shadow(scope, claim, now=2, report_sha256='invalid')
    assert store.get(scope, job)['state'] == 'LEASED'


def test_completion_rejects_expired_and_superseded_claims(tmp_path):
    store = store_at(tmp_path)
    scope = ScopeKey('profile', 'session')
    job = store.enqueue(scope, **observation())
    first = store.claim(scope, job, owner='one', now=100, lease_seconds=10)
    assert hasattr(store, 'report_shadow'), 'fenced shadow reporting missing'
    with pytest.raises(ValueError, match='stale claim'):
        store.report_shadow(scope, first, now=110, report_sha256=digest('report'))
    second = store_at(tmp_path).claim(scope, job, owner='two', now=110, lease_seconds=10)
    for bad in (first, dict(second, owner='one'), dict(second, fence=1), dict(second, attempt=1)):
        with pytest.raises(ValueError, match='stale claim'):
            store.report_shadow(scope, bad, now=111, report_sha256=digest('report'))
    with pytest.raises(ValueError, match='stale claim'):
        store.report_shadow(ScopeKey('other', 'session'), second, now=111, report_sha256=digest('report'))
    store.report_shadow(scope, second, now=111, report_sha256=digest('report'))
    assert store_at(tmp_path).get(scope, job)['state'] == 'SHADOW_REPORTED'
    assert store.claim(scope, job, owner='three', now=200, lease_seconds=10) is None
    with pytest.raises(ValueError, match='stale claim'):
        store.report_shadow(scope, second, now=112, report_sha256=digest('different'))


def test_claim_is_exclusive_and_restart_reclaims_with_new_fence(tmp_path):
    store = store_at(tmp_path)
    scope = ScopeKey('profile', 'session')
    job = store.enqueue(scope, **observation())
    assert hasattr(store, 'claim'), 'durable fenced claim missing'
    first = store.claim(scope, job, owner='worker-one', now=100, lease_seconds=10)
    assert (first['attempt'], first['fence'], first['lease_until']) == (1, 1, 110)
    restarted = store_at(tmp_path)
    assert restarted.claim(scope, job, owner='worker-two', now=109, lease_seconds=10) is None
    second = restarted.claim(scope, job, owner='worker-two', now=110, lease_seconds=10)
    assert (second['attempt'], second['fence'], second['owner']) == (2, 2, 'worker-two')
    assert restarted.get(scope, job)['attempt'] == 2
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT state FROM shadow_job_attempts ORDER BY attempt').fetchall() == [('EXPIRED',), ('LEASED',)]


@pytest.mark.parametrize('change', [
    {'terminal': 'running'}, {'ephemeral': True}, {'ephemeral': 0},
    {'run_id': ''}, {'stream_id': None}, {'algorithm_version': ''},
    {'capture_identity': ''}, {'capture_sha256': 'not-a-hash'},
    {'source_manifest': []}, {'source_manifest': [{'kind': 'sidecar'}]},
    {'unexpected': 'field'},
])
def test_invalid_observation_fails_closed(tmp_path, change):
    store = store_at(tmp_path)
    with pytest.raises(ValueError):
        store.enqueue(ScopeKey('profile', 'session'), **dict(observation(), **change))
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM shadow_job_observations').fetchone()[0] == 0


def test_conflicting_observation_is_rejected(tmp_path):
    store = store_at(tmp_path)
    scope = ScopeKey('profile', 'session')
    job = store.enqueue(scope, **observation())
    altered = dict(observation(), capture_sha256=digest('different'))
    with pytest.raises(ValueError, match='input conflict'):
        store.enqueue(scope, **altered)
    assert store.get(scope, job)['input_hash'] == digest(observation())


def test_same_observation_reuses_persisted_job(tmp_path):
    store = store_at(tmp_path)
    scope = ScopeKey('profile', 'session')
    first = store.enqueue(scope, **observation())
    assert store.enqueue(scope, **observation()) == first
    assert store.get(scope, first)['input_hash'] == digest(observation())
    assert store.get(scope, first)['state'] == 'PENDING'
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM shadow_job_observations').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM shadow_jobs').fetchone()[0] == 1
