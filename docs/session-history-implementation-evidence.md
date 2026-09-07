# Stable history implementation evidence

Design baseline: 80139190459b3de1e0b6a0913cecf877394439fc.
Status: isolated shadow tracer only; NOT production publication, NOT full M0 completion.

## Batch 1

- New `api/display_history.py`: independent SQLite immutable merged-history
  capture, one transaction for rows/tools/seal, persisted visible-row/tail
  indexes, scoped generation paging without source reads or full-history merge.
- Separate `shadow_*` namespace: deliberately not a replacement migration for
  the reviewed protocol schema. No claim that protocol jobs/fences/CAS exist.
- `read_candidate` explicitly returns the legacy routing decision for every
  scope. There is no enable flag and no production route wiring.
- Sources, existing GET, writers, Agent, live state and backups unchanged.

TDD actual execution via `./scripts/test.sh`:
1. RED: missing history module assertion (1 failed).
2. During implementation: fixture import corrected; actual GET comparison
   caught `_messages_truncated` mismatch. Old GET means offset > 0, not simply
   any window; implementation corrected without changing oracle.
3. GREEN: history + neighboring tail payload tests, 10 passed.
4. RED: late row insert into completed generation did not raise (1 failed,
   1 passed). Added persistent seal and insert guards.
5. GREEN: history + tail payload, 11 passed in 18.39s.

Tests use the actual legacy `handle_get` merge/window path with isolated fixture
source adapters, not production captures. Atomicity test uses a real SQL trigger
failure after the first candidate row and verifies no partial generation remains.
The test environment emits existing missing-requests plugin warnings; not a clean
production runtime certification.

## Batch 2

- Persisted scene records with ref/index lookup, page-local legacy scene hydration,
  immutable scene guards; sealed-only reads reject incomplete generations.
- RED: missing scene capture assertion (1 failed / 2 passed); GREEN 22 passed
  including tail/renderable neighbors. RED: unsealed generation incorrectly
  readable (1 failed / 7 passed); fixed by joining immutable seal on reads.
- Additional characterization tests (already green, not claimed as new RED):
  empty/tool-only/matched and orphan tool rows, bounded long tool content,
  duplicate text with active=0/1, before bounds, cross-profile generation denial,
  SQL immutability, payload budget, actual EXPLAIN indexed visible-row search.
- Final targeted + neighboring command:
  `./scripts/test.sh tests/test_display_history.py tests/test_session_tail_payload.py tests/test_session_message_window_renderable_tail.py tests/test_session_lineage_full_transcript.py tests/test_truncate_session_at_keep.py -q`
  returned **40 passed in 25.17s**. Ruff on both new Python files and
  `git diff --check` passed. Full suite and production data NOT_RUN.
- Scene coverage is a basic settled scene, not certification of every scene
  schema/tool/duplicate-ref combination. Parent and truncation tests in the
  command are existing legacy regressions, NOT new end-to-end capture coverage.

## Batch 3: detached real-source capture and complete differential CLI

- `api/history_capture.py` reads sidecar ancestry without `Session.load` (which
  can self-heal/save), and all columns of scoped SQLite rows in one `mode=ro`,
  `query_only` transaction. Both active and inactive raw rows are retained in
  the capture; the existing legacy projector excludes inactive rows for display.
- Capture hash binds scope, raw sidecar hashes/identities, parsed objects, DB
  schema and scoped rows. `shadow_captures` binds this manifest to the generation
  in the same sealed transaction; supplied messages/tools/scenes must equal the
  source-derived payload. Capture hashes are integrity checks, NOT signatures,
  cross-store snapshots, writer fences, or production eligibility.
- `compare` walks **every** cursor down to zero for every requested limit and
  records hashes, offsets, row counts, and differing field names. It uses the
  real legacy limited-display merge/window/hydration helpers over detached source
  objects, NOT the live HTTP GET (GET can recover/save). It intentionally does
  not certify live cache/backstop optimizations, redaction, or model metadata.
- Missing/cyclic/too-long/cross-profile ancestry, active source, missing DB,
  unsupported schema, foreign/CLI adapter, byte budget and integrity failures
  block capture/comparison. No best-effort partial history is marked complete.
- TDD: missing capture RED; missing comparison RED; scene/tool fixture caught a
  real bug: scene hydration needs absolute tool anchors, not rebased page anchors.
  Fixed shadow page hydration. Read `atime` changes initially caused a false
  mutation alarm; stable identity/size/mtime/ctime comparison fixes it.
  CLI missing-artifact RED; unrelated-payload binding and cross-profile RED;
  subsequent targeted/neighboring run: **50 passed in 43.94s**, Ruff and diff check
  passed. Failure-report traversal test added as characterization (not a new RED).

### Reproduce offline (private files; never commit captures)

```sh
# Capture does not import the runtime. Use an approved backup/isolated source.
.venv/bin/python -m api.history_capture capture \
  --session-dir "$BACKUP_SESSIONS" --state-db "$BACKUP_DB" \
  --session-id "$SESSION_ID" --profile-identity "$PROFILE_IDENTITY" \
  --max-bytes 150000000 --output "$PRIVATE_DIR/capture.json"
HERMES_HOME="$PRIVATE_DIR/isolated-home" \
HERMES_WEBUI_STATE_DIR="$PRIVATE_DIR/isolated-state" \
.venv/bin/python -m api.history_capture compare \
  --capture "$PRIVATE_DIR/capture.json" --output-db "$PRIVATE_DIR/shadow.db" \
  --limits 1,20,80,500 --max-bytes 150000000 --output "$PRIVATE_DIR/report.json"
```

Outputs must be new paths. JSON artifacts are mode 0600; keep the entire output
folder private (the shadow DB contains full transcript). Exit 0 = completed PASS,
1 = completed differential mismatch, 2 = blocked input/operation. Compare requires
explicit isolated runtime homes. This module uses scoped temporary adapters for
legacy source lookup and is offline/single-process only, never import it into a
running request worker as a concurrent capture service.

### Real historical evidence

Read-only SSH streamed capture from an approved backup's sidecar plus restored
SQLite copy; no script installation, remote file creation, or live GET invoked.
Selected capture SHA256:
`70902b9a982d90b176fef1bf11e0cfced6fb4b6b8463d2c9da4ad8fdd24ab08a`.
Raw capture 9,731,833 bytes, 1,042 DB rows (364 inactive), 21 scenes, 992 merged
messages, truncation metadata present. Limits 1/20/80/500 completed 545/28/7/2
pages respectively: **582 page comparisons PASS**, all routing LEGACY.
The first real parent-chain selection correctly BLOCKED on a missing ancestor;
no parent was fabricated or silently discarded. Complete parent chain is covered
by on-disk tests, NOT certified on that incomplete real lineage. Existing missing
`requests` plugin warnings remain in this isolated environment.

## Blocking / remaining

- Actual source writer closure and OS write exclusivity unproven: all scopes
  remain LEGACY. No mtime/hash watcher is substituted for pre-write invalidation.
- Protocol schema/invariant_check, mutation binding/pre-write invalidation,
  controlled source gate, worker lease/fence/CAS and production pointer publication
  are not implemented in this batch. Shadow atomic materialization is not that gate.
- Foreign/CLI source-specific capture adapters, full live-GET differential,
  complete real parent lineage and production performance budgets remain.
  Batch 3 covers detached WebUI history, not journal/context recovery certification.
- Candidate byte limit only bounds serialized payload; it is not the complete
  reviewed RSS/disk/WAL/concurrency budget. Production backfill remains blocked.
- No active events, compatibility receipts, recovery or new GET enabled.
- No deployment, no remote data operations, no backup/database deletion.
