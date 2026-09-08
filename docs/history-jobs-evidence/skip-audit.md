# Frozen full-suite skip audit

Contract Routing: test evidence only. No runtime, GET, worker or production state changes.

Source: the completed 16-shard run at `4127d0e7`, not the earlier interrupted Node-missing run. Its frozen manifest accounted for 15,245 unique nodeids: 15,139 PASS, 21 FAIL and 85 runtime SKIP. These are historical counts, not acceptance of subsequent changes.

Programmatic inspection of every shard event log finds exactly 85 runtime skip records and 85 unique skipped nodeids, with no xfail assumption. Full per-nodeid records and SHA-256 of all input logs are retained in `/workspace/history-failure-evidence/resume-skip-audit/audit.json`.

| Recorded cause | Runtime skips | Acceptance consequence |
|---|---:|---|
| Playwright unavailable | 68 | Browser behavior unverified; dependency setup and real browser execution required |
| Empty skills list | 4 | Unresolved; skip text speculates about pollution but is not proof of its cause |
| Windows-only | 2 | Not exercised on Linux |
| macOS-only | 2 | Not exercised on Linux |
| Spawn import environment / editable Agent | 2 | Cron child-process behavior unverified in this environment |
| ESLint unavailable | 2 | JS runtime/scope lint unverified |
| Default port occupied | 1 | Do not interrupt the existing service to force the test |
| Explicit non-TTY timing-flaky skip | 1 | Remains unverified, not converted to PASS |
| CLI package not installed | 1 | Installed entry-point proof unavailable |
| Docker opt-in | 1 | Docker integration not exercised |
| ACL tools unavailable | 1 | ACL preservation not exercised |

Separately, each shard repeats the same collection skip for `tests/manual/cdp_native_scrollbar_test.py` because `websockets` is unavailable: 16 reports, one unique collection node. These are NOT added to the 85 runtime skips.

## Risk, response and verification

- Risk: summing XML suite totals, subtest reports and repeated collection skips creates contradictory counts. Response: enumerate frozen nodeids and phase reports; retain the collection ledger separately. Verification: exact unique runtime count is 85 and categories sum to it.
- Risk: 'likely pollution' becomes an invented root cause. Response: classify empty-state skips as unresolved until seeded or ordered reproduction proves the cause.
- Risk: installing optional browser tooling in a shared interpreter while tests run changes their environment mid-run. Response: do not mutate the active test environment during concurrent repair runs; schedule optional gates under a fixed toolchain afterward.
- Risk: accepting platform or dependency skips as full validation. Response: keep the unexecuted acceptance boundary explicit; no deployment or new-read enablement follows this audit.

This closes classification/accounting only. It does not close the skipped test coverage or certify a green full suite.
