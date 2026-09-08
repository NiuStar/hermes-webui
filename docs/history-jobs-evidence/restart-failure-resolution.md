# Shadow history restart test: failure resolution

Scope: frozen baseline `4127d0e7`; test harness only. No production deployment,
GET enablement, worker integration, real-home writes, or database deletion.
`api/history_jobs.py` and shared `tests/conftest.py` remain unchanged.

## Root cause and isolation

The original shard-12 stderr, and a fresh replay of that shard, show
`ModuleNotFoundError: No module named 'tests.test_history_jobs'` in
`multiprocessing.spawn._main`, while unpickling the target. The child never
enters `_child_claim`; SQLite recovery and network calls are not reached.
The parent waits on `Queue.get(timeout=20)` and hides the import failure as
`_queue.Empty`.

A single restart test passes. Running the atomic-enqueue test immediately before
it fails. `tests/conftest.py` snapshots `_REAL_SYS_PATH` and restores it after
every test. In this environment that snapshot starts with the agent checkout,
which contains a different `tests/__init__.py`. The parent already has WebUI's
`tests` cached; a fresh child instead resolves the agent's `tests` package.
A fresh-interpreter probe using the parent's exact path confirms that
`find_spec('tests.test_history_jobs')` is `None`.

The network failure has a separate mechanism: importing `server.py` during full
collection with `HERMES_WEBUI_TEST_NETWORK_BLOCK=1` installs
`server._blocked_create_connection`, replacing the conftest wrapper expected by
the name-based assertion. Full collection with only the restart test selected
passes even with this server wrapper installed. The two-test history-only
reproducer fails with the correct conftest wrapper still installed. Thus the
network wrapper is neither necessary nor sufficient for the restart failure;
collection/teardown order merely exposes both in the same run. Network isolation
alone passes its full file. Shared network-wrapper ownership/fixture restoration
is explicitly left for the parent task; no shared conftest change was made.

## Minimal fix and regression

Only the spawn-time path snapshot is pinned to this repository using a scoped
`monkeypatch.context().syspath_prepend`. Parent ordering is restored immediately
after `process.start()`. The child is joined before receiving its single small
claim, so an import crash reports exit code and captured stderr rather than a
misleading queue timeout. Process and queue cleanup now runs in `finally`.
Timeouts remain 20 seconds; no skips were added.

A deterministic regression creates another checkout containing its own `tests`
package, puts it first, runs the real spawned SQLite claim/restart sequence, and
asserts parent path preservation and no remaining child process. Disabling only
the path pin makes this regression fail with child exit code 1 and the exact
`ModuleNotFoundError`; restoring it passes. The existing recovery assertions
remain: persisted job discovery at lease expiry, fence increment to 2, and exact
observation round-trip.

## Evidence and reproducibility

Local evidence root: `/workspace/history-failure-evidence/restart-resolution/`.
Runner: `/workspace/history-failure-evidence/restart_run.py TAG PYTEST_ARGS...`.
It calls `./scripts/test.sh`, strips credential/provider variables, and assigns
separate HOME, HERMES_HOME, HERMES_BASE_HOME, config and WebUI state paths for
each run, following `gate_run.py`. Every run keeps combined stdout/stderr,
JUnit XML, command arguments, elapsed time, and return code. Sandboxes and all
database artifacts are retained. No application server was deployed.

Key evidence:

- `single-red.*`: original test alone passes (the tag denotes the baseline, not a failure).
- `pair-probe.*` and `import-probe.json`: minimal order reproducer fails;
  fresh child resolves agent `tests`, while socket wrapper is conftest's.
- `collection-probe.*` / `collection-import-probe.json`: restart alone after
  full collection passes despite server network wrapper.
- `collection-red.*`: full collection, network assertion then restart: both fail.
- `shard12-red.*`: replay of original failing shard, restart failure reproduced.
- `shadow-control-red.*`: disabling only the path pin reproduces child import
  failure, now with explicit exit code 1 and captured stderr.
- `history-green.*`: initial fixed history jobs and adjacent capture/display tests:
  51 passed.
- `network-alone.*`: 10 passed, establishing the independent collection effect.

## Final verification

- `final-history-green.*`: **62 passed**, including the new adversarial package
  shadowing test plus all history jobs, capture, display and network-isolation
  tests in that command.
- `collection-green.*`: restart now **passes** in the same full-collection/two-test
  reproduction; the independent network wrapper-name assertion still fails.
- `shard12-green.*`: original shard replay now has **953 passed, 6 skipped,
  1 failed** among 960 enumerated XML testcase elements (959 selected tests plus
  a collection skip). Before the fix: 952 passed, 6 skipped, 2 failed. The sole
  remaining failure is
  `tests/test_ctl_script.py::test_start_can_ignore_repo_dotenv_for_authoritative_test_env`,
  outside this task. The restart test passes. This shard was collected before
  adding the new adversarial test; that new test is covered by the final 62-test
  focused run instead.
- `verified-accounting.json` contains programmatically enumerated testcase
  accounting. JUnit suite-level totals on the shard include subtest accounting
  and differ from the testcase count, so those totals are not substituted for
  enumerated results.
- `ruff check tests/test_history_jobs.py` and `git diff --check`: passed.

No claim of a clean full suite is made. The network fixture and ctl-script
failures remain independently reproducible; no production code was changed.
