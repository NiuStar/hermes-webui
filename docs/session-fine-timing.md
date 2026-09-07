# Opt-in session GET timing

Instrumentation only, based on production af654091; no cache, storage, model lookup or transcript semantics changed.

Set `HERMES_WEBUI_SESSION_FINE_TIMING=1` and send an authenticated GET `/api/session` with `X-WebUI-Timing-ID` matching `[A-Za-z0-9_-]{1,48}`. Without both gates, no fine timing is collected. One JSON log per opted-in request, at most 128 spans and 64 numeric/boolean counters; no transcript, provider credentials or URLs. Existing slow diagnostics remain unchanged.

`inclusive_ms` includes nested calls; `exclusive_ms` subtracts immediate children. Sum exclusive durations once, then add `unattributed_ms` to recover request elapsed time. Unattributed includes unwrapped route work. `agent.get_model_context_length` measures the real Agent function, **not proof of network latency**. JSON response timing includes serialization and socket write. Cache byte size is captured from the existing calculation, never serialized again for diagnostics.

Tests: deterministic nested arithmetic; disabled/non-session/invalid-ID gates; exception cleanup, log failure, bounded span retention; neighboring display cache and watchdog regressions.

Production acceptance evidence is kept outside the source tree (private host/session identifiers). Baseline and diagnostic requests use identical `msg_limit=30` windows, serial repetitions for both model-resolution toggles, canonical message hashes/counts and health/active-run checks. No cache clearing or data deletion. Only restart after verified idle.
