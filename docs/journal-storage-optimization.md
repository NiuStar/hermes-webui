# Lossless storage optimization and rollback

Session sidecar bodies use compact JSON whitespace; metadata prefixes retain their existing scanner-compatible layout. Values, model context and transcript history are not removed. Display and lineage caches each have a 32 MiB serialized-byte budget (not an RSS guarantee) plus their existing entry limits.

New large process journal payloads may use independently decodable `zlib-base64-json-v1` rows. Live SSE and replay return the original dictionary. Terminal and ephemeral events retain their prior representation; expanded replay bytes count toward replay limits. Existing journals and SQLite history are not rewritten. This does not deduplicate compaction snapshots in state.db.

## Mandatory rollback gate

**Do not deploy an older image lacking `_decode_event_payload` after encoded rows have been written.** Setting `HERMES_WEBUI_JOURNAL_COMPRESSION=0` stops new encoded writes but does NOT make existing rows readable by old code. Operational rollback must retain this release's `api/run_journal.py` decoder and replay paths, with compression disabled. Revert only `api/models.py` and `api/routes.py` if those optimizations need rollback, build a new immutable image, and verify existing encoded replay before switching. Never delete journals/history or restore an old state database to bypass this gate.

The emergency no-code rollback is this same release image with `HERMES_WEBUI_JOURNAL_COMPRESSION=0`; it retains all decoders. Preserve the prior image and compose manifest as evidence, not as an automatically safe rollback target.
