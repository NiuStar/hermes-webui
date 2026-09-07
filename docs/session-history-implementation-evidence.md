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

## Blocking / remaining

- Actual source writer closure and OS write exclusivity unproven: all scopes
  remain LEGACY. No mtime/hash watcher is substituted for pre-write invalidation.
- Protocol schema/invariant_check, mutation binding/pre-write invalidation,
  controlled source gate, worker lease/fence/CAS and production pointer publication
  are not implemented in this batch. Shadow atomic materialization is not that gate.
- Scene hydration, full old-source capture/manifest, parent/truncation/active=0
  integration, real dataset differential command and performance budgets remain.
- Candidate byte limit only bounds serialized payload; it is not the complete
  reviewed RSS/disk/WAL/concurrency budget. Production backfill remains blocked.
- No active events, compatibility receipts, recovery or new GET enabled.
- No deployment, no remote data operations, no backup/database deletion.
