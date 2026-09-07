# Next storage optimization: execution and gates

Status: approved scope; implementation staged, not all gates passed.

## Inspected baseline

Baseline `af654091`: durable journals already append one independently decodable event per call, reserve sequence once per process, and compact successful done records. Ordinary Agent flush already uses `_db_persisted`; reimplementing either is not an optimization. Token and reasoning producers currently emit deltas, not cumulative snapshots. Whitespace reduction does not resolve duplicate bodies.

The retained production long sessions have 90,966,384 / 83,789,114-byte sidecars. The first has truncation watermark and boundary, 27,682 merged rows and a 126-row window for 60 visible rows. Its 32 MiB merge cache rejects the entire transcript, so every warm request repeats reconciliation. Instrumented read: 8.814s with cProfile overhead, including 3.965s merge and 1.999s cache-size serialization. This is not an HTTP latency result.

## Execution order

1. Preserve existing data, backups, journals, staging and source ownership. Only existing production deployment route; no extra deployment environment.
2. Small read-only candidate: permit bounded, losslessly encoded oversized display-merge entries, retain exact existing provenance validation and fallback. Cap expanded representation as well as retained bytes; never change visible pagination or truncate authoritative bodies. Test nested mutation isolation and invalidation. Measure cold and warm separately; this does not promise cold first-screen SLO.
3. Journal incremental persistence: audit actual producers/readers before introducing a format. Preserve existing event IDs, crash flush policy, independent decoding and error payloads. Any new cross-event delta format requires all readers plus compatible rollback artifact; do not ship a speculative second format to claim completion.
4. Design/review immutable bodies and context membership below before changing Agent storage. Do not retire duplicate sidecar writes until all authority/compatibility gates pass.
5. Focused and neighboring tests, self-review (independent child unavailable), commit/push verified SHA, immutable production build, idle switch, API/history parity and recovery readback. Retain evidence reports. All unmet acceptance criteria remain open.

## Immutable body / context version design (review before implementation)

Proposed Agent-owned additive tables:
- `message_bodies(body_id, origin_store, origin_message_id, schema_version, body_json)`: immutable complete message values. Identity comes from the actual originating message, not a content hash. Identical text in different turns remains distinct.
- `context_versions(version_id, session_id, parent_version_id, created_at, reason)`: context publication history.
- `context_members(version_id, ordinal, body_id)`: ordered membership, unique `(version_id, ordinal)`; a body may occur in several versions.
- Session points at a committed active context version. Publication of bodies, complete memberships and pointer occurs in one transaction. Readers order by ordinal, never body ID.

Compaction inserts only genuinely new summary bodies and reuses explicit source identities for unchanged tails. Old versions and all historical original rows remain intact. Rewind/regen forks a version, never mutates a body. Tool IDs, reasoning, multipart content, timestamps, provider fields and unknown fields must survive unchanged. Missing identity must preserve separate bodies, not infer equality from content. FTS ownership, search display and export need explicit adapters.

### Self-review verdict: NOT READY to mutate production

The inspected WebUI repository does not own the running Agent schema (`state.db` version 26) or its CLI/gateway/search/export readers. Existing replay uses `ORDER BY id` plus active flags; merely reactivating an old tail places it before the new summary. A WebUI-only table cannot safely change that contract. Required gates: exact Agent source revision pinned in release, full producer/consumer inventory, additive migration/dual-read implementation in Agent, old/new output parity on retained histories, failure injection during transaction, context-order and compaction identity tests, rollback reader supporting newly published versions. No destructive migration or historical deletion is authorized.

### Sidecar body retirement: blocked by authority gate

Sidecars retain WebUI-only message fields, anchor scenes, recovery and lineage semantics. Disabling writes before the canonical body store covers those fields and save-failure recovery loses data. Require per-field mapping, atomic reference publication, missing-body fail-closed behavior, restart/rollback/export/import/continue parity, and actual retained-production conversation evidence before switching writes. Historical duplicate rows cannot be reclaimed without separate deletion approval.

## Acceptance labels

Report separately: deployed code, warm-read improvement, cold-read SLO, journal write/storage reduction, immutable-body implementation, sidecar retirement, actual recovery. A passing cache test is not proof of browser usability, storage deduplication or migration completion.
