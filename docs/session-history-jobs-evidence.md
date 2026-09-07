# Independent durable shadow job slice

Status: executable shadow task state machine only; **GET LEGACY / NO DEPLOYMENT**.
Baseline reviewed boundary: `e14e44b462c6a3b073f601b8bb3cc8a8df96142a`.

## Contract Routing and boundary

State mutated: only explicitly supplied independent SQLite task database.
References: coding-transactions §§1,4–6 and coding-writers §§2–3;
reviewed coding-schema remains a separate, unfinished production protocol.
This `shadow_job_*` namespace is NOT its migration or a reduced replacement.
No streaming/routes/source-writer edits, callbacks, production source reads,
service restarts, database/backup deletion, or deployment are part of this slice.

## Implemented

- `api/history_jobs.py`: explicit `ScopeKey(profile_identity, session_id)` on
  every API; immutable observation binds run/stream, terminal, operation key,
  capture identity/hash, source identity/kind/hash list, algorithm version and
  explicit `ephemeral=False`. Terminal values: `normal`, `cancelled`, `failed`.
  Invalid/unknown fields and ephemeral inputs reject before insertion.
- Logical uniqueness is `(scope, operation_key)`; caller must persist/reuse the
  same operation key. Same canonical observation hash returns the existing job;
  different input rejects without overwriting. Scope is the composite database
  key, input hash covers the observation JSON. Capture references/hashes are
  declared observations, NOT proof that the capture was loaded or authenticated.
- Observation and PENDING job insertion commit together under BEGIN IMMEDIATE.
  Claim is another short transaction: increments attempt/fence, persists owner
  and lease, retains previous attempts as EXPIRED on reclamation. No build or
  source work occurs while that transaction is open.
- `ready(scope, now=...)` enumerates persisted PENDING / expired LEASED jobs.
  Fresh store/process can recover these without running agent tools. Explicit
  `now` is a trusted scheduler epoch clock, NOT HTTP/client input. Clock rollback,
  trusted scheduler integration and lease renewal are not certified here.
- `report_shadow` verifies exact scope/job/attempt/fence/owner/lease and strict
  unexpired deadline. The report hash and state transition commit together.
  Wrong profile/owner, old attempts, expired claims and second reports reject.
  `SHADOW_REPORTED` means only **unverified report recorded**, not DONE,
  VERIFIED, SEALED or PUBLISHED. Reports and observations cannot be updated/deleted
  through ordinary SQL. No candidate-generation validation is replaced by this
  marker; no production consumer exists.
- API transaction boundaries enforce job/attempt transitions. This is not
  protection against arbitrary same-UID SQL writers or a complete protocol
  trigger/invariant_check implementation.

## Actual RED / GREEN evidence

All commands used `./scripts/test.sh tests/test_history_jobs.py -q` (later RED
runs added `--tb=short`). Logs are local `/workspace/history-jobs-*.log`.

| Cycle | Actual RED | Actual GREEN |
|---|---|---|
| 1 persisted idempotent enqueue | 1 failed: module missing assertion | 1 passed, 16.40s |
| 2 conflicting hash | 1 failed / 1 passed: DID NOT RAISE | 2 passed, 15.67s |
| 3 observation validation | 11 failed / 2 passed: DID NOT RAISE | 13 passed, 16.60s |
| 4 claim and fencing | 1 failed / 13 passed: claim missing | 14 passed, 15.33s |
| 5 stale report rejection | 1 failed / 14 passed: report API missing | 15 passed, 16.70s |
| 6 lease/report validation | 6 failed / 15 passed: DID NOT RAISE | 21 passed, 15.88s |
| 7 persisted ready scan | 1 failed / 21 passed: ready missing | 22 passed, 16.62s |
| 8 immutable observations/reports | 1 failed / 22 passed: DELETE accepted | 23 passed, 19.07s |

Additional characterization tests (already green, NOT claimed as additional RED):
real SQLite trigger faults at enqueue/claim/report roll back intermediate writes;
barrier-synchronized competing connections converge on one enqueue/one claim;
spawned process commits then exits normally and a fresh instance reclaims from
persistent state; normal/cancelled/failed inputs accepted. No actual service was
killed. This proves process restart recovery, not power-loss durability.

Neighbor command:

```sh
./scripts/test.sh tests/test_history_jobs.py tests/test_display_history.py \
  tests/test_history_capture.py tests/test_session_tail_payload.py \
  tests/test_session_message_window_renderable_tail.py -q
```

Result: **70 passed in 48.87s** (`/workspace/history-jobs-neighbors.log`).
Ruff on both new Python files and `git diff --check`: passed.
Existing missing-`requests` plugin setup warnings appeared in RED logs; these are
not a clean production-environment certificate.
Full-suite attempt: `./scripts/test.sh -q`, local log
`/workspace/history-jobs-full-suite.log`. **NOT PASS / incomplete**: collected
15,306 tests, then interrupted only the spawned pytest PID with SIGINT after
unrelated frontend/environment failures were evident. Final real output:
`28 failed, 1785 passed, 181 skipped, 9 warnings, 7 errors in 386.05s` (exit 2).
Log includes `FileNotFoundError: node`, missing-Agent skips and existing Python
escape warnings. Other reported failures have not been individually diagnosed;
no claim all failures are pre-existing or caused solely by Node absence.
No production service was signalled. Targeted/neighboring 70-test run above is
the completed acceptance evidence, not a substitute full-suite pass.

## Remaining blocking work — NOT_IMPLEMENTED

1. Reliable runtime terminal-discovery receipts and restart backscan for terminal
   observations not yet enqueued. `ready` only scans already durable jobs; no
   streaming callback is presented as automatic durable discovery.
2. Real capture loading/revalidation and generation builder integration. Source
   manifest is a normalized reference list, not the full reviewed receipt schema.
3. Atomic candidate sealing + task finalization across task/history databases.
   Therefore no complete-build/publish API exists. Generation provenance,
   integrity, terminal coverage, sealed segments and source-manifest validation
   must remain mandatory; a task report is not substitute evidence.
4. Reviewed mutation epoch/revision/base/SEALED authority, budgets, source gate,
   full schema triggers/invariant_check, writer closure and OS exclusivity,
   compatibility receipts, production publication CAS and rollback barrier.

No enable flag is introduced; all production read routing remains LEGACY.
