# Segmented Session Sidecars and Bounded History Loading

- **Status:** Implemented
- **Author:** Nous Research / Hermes Agent
- **Created:** 2026-09-13

## Problem

A WebUI session sidecar is a single JSON document. Context compression can reduce
`context_messages` while the visible `messages` transcript remains cumulative.
Long-running sessions therefore retain tens of thousands of already-compacted
rows in one file. Before this change, even `GET /api/session?msg_limit=30`
materialized the whole sidecar and reconciled it with `state.db` before slicing
the response. Concurrent cold loads multiplied JSON-object memory and could OOM a
4 GiB WebUI container.

The visible transcript is user data. Context compression is not permission to
delete it. A safe fix must preserve every visible row while bounding server-side
read, write, and response memory.

## State ownership

- Session sidecars and their compression-snapshot lineage are authoritative for
  the visible WebUI transcript.
- `context_messages` remains the model-context projection and may shrink after
  compression without changing visible-history ownership.
- Hermes Agent `state.db` remains an independently durable runtime source. A
  bounded sidecar read may be used only when active `state.db` rows are proven to
  be a multiset subset of the indexed logical sidecar history.
- `.message_offsets/<session_id>.json` is a disposable derived index. It contains
  byte ranges, counts, timestamps, and SHA-256 message-identity digests, but no
  transcript body. It is never authoritative and may be deleted/rebuilt.
- `<session_id>.json.bak` remains a recovery artifact. This design does not
  weaken intentional-shrink or startup-recovery safeguards.

## Storage format

A compression continuation may persist a physical segment instead of copying its
entire logical history. The child records:

- `parent_session_id`: the preceding physical segment;
- `lineage_parent_overlap_count`: the number of child-prefix rows already owned
  by the parent snapshot;
- `lineage_message_count`: total logical rows across the lineage.

The parent is first atomically persisted and read-back verified as
`pre_compression_snapshot=true`. Only then may the child persist a segment. If
the snapshot, marker, count, profile, source, or overlap cannot be proved, the
continuation uses the legacy cumulative write instead.

The parent snapshot is written after the compressed turn settles, so it already
contains the child's entire initial segment (compression marker plus the settled
turn). The overlap count is therefore the full initial segment length, not merely
one marker row. Later turns append rows unique to the child.

Repeated compression produces a bounded chain of physical segments; it does not
re-expand previous parents into each new file. Lineage traversal is cycle-safe,
profile-scoped, WebUI-source-only, and limited to 20 hops.

## Bounded reads

`Session.save()` streams JSON encoding and backups. It does not build complete old
or new sidecar strings.

Each sidecar has a stat-signature-bound offset index. Existing sidecars build the
index with a read-only `mmap` scan. The scan records message/tool/scene byte
ranges, normalized identity digests, and the latest message timestamp. Publication
is atomic and rejected if the sidecar changes during the scan.

Limited history requests decode only the requested logical window. Multi-segment
requests map the global cursor onto per-segment ranges and remove exact persisted
overlap. Tool-call and anchor-scene indexes are rebased to response coordinates.
Any malformed index, invalid range, changed file descriptor, profile/source
mismatch, cycle, missing snapshot marker, unproved `state.db` row, or exceeded
bound falls back to the serialized canonical path.

Hard bounds:

- response page: 500 visible rows;
- decoded indexed window: 5,000 rows / 16 MiB;
- derived index: 64 MiB / 100,000 indexed rows, enforced while scanning;
- lineage traversal: 20 segments;
- concurrent full sidecar resolutions: 1.

A bare message request against an oversized sidecar or oversized compression
lineage is server-bounded to 500 rows. Full history remains reachable by repeated
`msg_before` pagination; there is no query parameter that restores a one-response
unbounded transcript.

## Browser contract

Ordinary refresh, retry, undo, cancel, settled recovery, and compression preflight
use metadata-only or bounded requests. “Load all” and outline navigation assemble
history through `msg_before` pages.

A reloaded physical child can emit a terminal SSE payload containing only its
segment. Such payloads carry `_messages_segmented=true`. The browser merges the
segment with its current window by the longest strict contiguous identity overlap,
then appends only the new suffix. It preserves the existing pagination cursor.
A live continuation that still holds the complete logical transcript in memory is
not marked segmented and retains the original terminal replacement behavior.

## Recovery and rollback

- No existing authoritative sidecar is deleted by index creation.
- Existing large sessions may be repaired by creating a verified byte-for-byte
  backup and then building only the derived index; the sidecar body remains
  unchanged.
- On WebUI startup, a singleton background maintenance worker waits 15 seconds,
  then builds missing or stale derived indexes for idle sidecars of at least
  10 MiB. Smaller files still build indexes on demand. Sessions
  with pending metadata or a live writeback owner are skipped and retried every
  five minutes. The worker never rewrites or deletes authoritative sidecar JSON.
- Operators can audit the same path with
  `python scripts/repair_large_session_sidecars.py` (dry-run) and explicitly
  apply it with `--apply`. Both commands print aggregate counts only. Set
  `HERMES_WEBUI_SIDECAR_MAINTENANCE=0` to disable automatic maintenance.
- Physical segmentation occurs only at a future verified compression boundary.
- Removing the derived index is safe; the next bounded request rebuilds it.
- A failed snapshot or index build leaves the original sidecar path and logical
  transcript untouched and falls back to the canonical serialized read/write.
- Parent snapshots must not be deleted while descendants reference them.

## Observability

`GET /health?deep=1` reports aggregate-only storage diagnostics:
sidecar count, index coverage, counts above 10/25/50 MiB, maximum sidecar bytes,
full-resolution concurrency/inflight counts, and aggregate maintenance status.
It never returns session IDs,
titles, paths, or or transcript content.

## Acceptance

- Existing sidecars remain parseable and byte-identical when only an index is
  built.
- Indexed and canonical windows have identical normalized message SHA-256 values.
- Pagination crosses physical-segment boundaries without `Session.load()`.
- `state.db` rows absent from the logical sidecar reject the fast path.
- Sidecar/index atomic-replace races cannot bind old offsets to new bytes.
- Save, backup, tool-call coordinates, anchor-scene coordinates, todo snapshots,
  lineage counts, terminal SSE, cancellation, reconnect, and repeated-save paths
  have regression coverage.
- A 67.9 MiB / 31,502-row three-segment fixture cold-builds in bounded memory and
  serves warm 30-row windows without materializing the full transcript.
