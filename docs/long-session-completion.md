# Long-session completion and metadata reconciliation

The frontend stores `_metadataMessageCount` separately from `message_count`.
The former is the `messages=0` synchronization coordinate, the latter is the
merged display/pagination coordinate. A lineage merge can legitimately make
these differ. Never use the display count to compare metadata poll results.
No backend transcript rewrite or per-page recount is introduced.

A same-session reload stages `_pendingMetadataMessageCount`; it acknowledges
that baseline only after the message response is adopted. Failed reloads keep
the prior baseline, messages, and visible DOM. Cross-session loads still clear
the old conversation; empty-session failures still display an error.

A full terminal SSE snapshot is adopted through `_settledSessionForCurrentWindow`.
For the same paginated session, preserve the loaded absolute prefix boundary,
include appended rows, and clamp on shrink so the latest answer remains present.
Already windowed snapshots and other session IDs retain their own coordinates.
This bounds frontend adoption/rendering, not the server's full SSE wire payload.

## Verification

Run `node tests/long_session_settle.cjs` or its pytest wrapper. Neighboring
coverage includes external refresh, load isolation, scroll anchoring, worklog
settle, compact done payload counts, and failed reload recovery.

Browser checks must use real long-history data: observe rendered text, composer
button state, frame gaps, JS exceptions, and final-answer retention beyond the
post-stream cooldown. A backend `completed` flag alone is not acceptance.
Test normal completion, persisted-event replay, and an injected message-fetch
failure with a successful retry. Keep model calls isolated from user history.
