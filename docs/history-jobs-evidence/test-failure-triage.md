# Full-suite failure triage (not a green-suite certificate)

## Fixed revisions and scope

- Current: `82ce96beda8c700cfed379a527ca15d9d2344764`.
- Pre-history-implementation baseline: `f5730a66feca090f5684c6d966344eeda969e79f`, separate detached worktree, same Python environment.
- No production code changed, no deployment, no production database/backup/staging deletion.
- GET remains LEGACY. Terminal discovery/reconciliation and build worker remain blocked.

## Complete original-log inventory

The interrupted run collected 15306 items and reported **28 failed, 1785 passed,
181 skipped, 7 errors**. All 35 reported failure/error tracebacks end in
`FileNotFoundError: node`. This is now parsed evidence, not an assumption that
installing Node makes the entire suite green. Every nodeid and source line range
is recorded in `test-failure-inventory.json`; full tracebacks are retained in the
local `failure-inventory.json` under `/workspace/history-failure-evidence`.

Minimal no-Node reproduction (both fixed revisions):

```sh
./scripts/test.sh tests/test_api_timeout.py tests/test_cancel_stream_owner_guard.py --timeout=20 -q
```

Both: **3 failed, 12 passed, 7 errors**. Installed `nodejs-wheel==22.20.0`
into the existing repository `.venv` only, using its own pip. Node reports
`v22.20.0`. No global install or production environment modification.
With `.venv/bin` prepended to PATH, all six original failure modules pass:
**71 passed on current, 71 passed on baseline**. No application fix was needed.

## Additional failures exposed after Node

1. **Inherited recovery deletion guard**, not a history implementation regression.
   `test_delete_cli_session_artifact_cleanup_is_idempotent` fails on both revisions
   with runner `HERMES_WEBUI_DISABLE_SESSION_DELETE=1`. Product correctly refuses
   deletion. Running the module with `HERMES_WEBUI_DISABLE_SESSION_DELETE=0` scoped
   only to the isolated test process produces **23 passed on each revision**.
   The existing guard is not removed or weakened in application code.
2. **Inherited onboarding-open opt-in**, not a history implementation regression.
   `tests/test_passkey_auth.py` gives **2 failed, 15 passed** on each revision with
   runner `HERMES_WEBUI_ONBOARDING_OPEN=1`. The two tests expect remote bootstrap
   to be denied, but the operator opt-in explicitly allows it. Process-local
   `HERMES_WEBUI_ONBOARDING_OPEN=0` produces **17 passed on each revision**.
   No auth behavior or assertion changed.
3. **Overly short diagnostic timeout**: an exploratory 10-second per-test limit
   interrupted the session server fixture before readiness. The exact first
   test passes with the repository's normal 60-second limit. Do not treat the
   exploratory timeout as a product failure or retain that shortened gate.

These are three separately observed causes; Node alone is not the complete
full-suite diagnosis. No confirmed new product regression has yet been isolated.

## Regression evidence and remaining block

- `tests/test_history_jobs.py tests/test_history_capture.py tests/test_display_history.py`:
  **51 passed**, normal 60-second test timeout.
- Bounded shard 0/16 with Node, normal timeout, inherited flags: **1 failed,
  150 passed, 4 skipped**, stopped at deletion guard conflict.
- With only deletion override: **1 failed, 608 passed, 4 skipped**, stopped at
  onboarding-open conflict.
- With both process-local overrides: reached 90% without a reported failure,
  then the 240-second overall process budget expired at
  `TestTLSEndToEnd::test_tls_startup_failure_fallback_to_http`.
  That exact test **passes alone** with a 60-second test timeout. This does not
  establish shard completion or rule out order-dependent trouble.
- Earlier shorter-budget experiments also ended with exit 124; their logs are
  retained, not counted as passing runs. One mistaken targeted command named a
  nonexistent test file, returned collection exit 4, and was corrected before
  the successful 51-test run.
- Agent discovery warns that agent-dependent tests are unavailable. Server boot
  logs additionally show missing `requests` in the isolated Python environment;
  no broad agent dependency installation was attempted.

**Full suite is NOT green and was NOT fully executed.** Remaining shards and
order-dependent/slow boundaries still require bounded continuation. The original
run and later collection report different collection counts; these are not
silently equated. A future complete run must freeze one collection manifest and
account for every nodeid rather than add totals from overlapping runs.

Local raw logs/XML and full inventory: `/workspace/history-failure-evidence/`.
`test-failure-run-index.json` records retained log hashes and real summary lines.
A generated temporary `.cjs` from an interrupted test was left untouched in the
worktree and is not committed.

## Continuation constraints

Use `./scripts/test.sh`, repository-local Node PATH, isolated fixture state,
normal `--timeout=60`, bounded batches with durable logs. Scope the two confirmed
runner-flag corrections to test subprocesses only; never change live settings.
Do not call SHADOW_REPORTED a successful build, or job readiness terminal-state
reconciliation. Terminal discovery/repair and concurrent build worker are not
implemented or authorized by this evidence checkpoint.
