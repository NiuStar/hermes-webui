# Next optimization candidate acceptance — 74a8c287

## Verdict

**Candidate deployed, formally rejected, production restored to decoder-compatible af654091. Overall requested optimization remains incomplete.** No migration, historical deletion, new test deployment, Agent source change or sidecar retirement was performed.

## Delivered artifacts

- Design and pre-implementation review: `docs/next-storage-optimization.md`, commit `6dca3220599a701e26fbbb67914a8dc3f206afe1`.
- Implemented bounded lossless encoded oversized display cache in `api/routes.py`; existing full provenance validation and visible row semantics preserved. Added encoded-cache tests and expanded-limit coverage. Commits `0b046e2a57fa7056f920fdc5b22e0a55b4516f72`, `74a8c28756ad79c512f77af8f92cdf73f5dbe459`.
- 136 focused/neighboring tests passed; fixture-only follow-up 4 passed. Diff-scoped Ruff: zero new violations. Initial fixture import/lint errors and two provenance revalidation failures were fixed before deployment.
- Candidate archive SHA256 `2764ef7d3401081a14ec150ef8030468d3591a7e673799440ceb635f0e107887`, verified after resumed transfer.
- Candidate image `sha256:556b51aa3a1dd9c470414c4617e015074d6f6017506d5d5308a514130dfbd6f1`; runtime `/app/api/routes.py` and image `/apptoo/api/routes.py` same SHA256 `70c88ec3c36a6d7adea887ea1d76e0f65dcf73ac1b3dfb8ad5d8e6d5222ef1c1`.

## Evidence and rejection reason

Actual retained-history in-process probe returned exactly 27,682 rows on three candidate warm reads (0.589/0.672/0.597 seconds). Initial per-token Python encoding experiment increased cold work and was replaced with per-row C encoding. These are not browser timings or storage savings.

Authenticated production API before/candidate/warm readbacks retained exact message JSON SHA256 across all sampled windows: 126, 169 and 10 rows. The two large sidecar SHA256 values remained identical. Database 499 sessions / 461,491 messages, `quick_check=ok`, sidebar 484 visible sessions before and after. No bodies or source classifications rewritten.

**Production HTTP acceptance did not reproduce a reliable end-to-end improvement:** first post-start long request 18.096s, second session 6.039s; later warm long requests 1.778–2.845s. The retained small acceptance conversation regressed from 0.059–0.070s before to 1.064–3.177s in the later candidate run. Startup dependency installation and model/provider lookup are possible confounders, not established causes. Therefore the HTTP latency gate failed and the candidate was not left running. Browser interactivity SLO and new provider conversation were not claimed; no new acceptance turn was needed to justify rejection.

Rollback used the known journal-decoder-compatible af654091 image, NOT an older decoder-incompatible release. Candidate compose and prior compose retained. Existing retention was 8 runs / 256 MiB; raised to 1,000,000 runs / 64 GiB during switch and retained on rollback so controlled operations cannot trigger historical pruning. This is temporary suppression, not final bounded-storage acceptance.

## Unmet gates and exact blockers

1. Cold bounded tail: actual long session has both truncation boundary and watermark; a naive tail query skips required historical reconciliation. Current candidate still fully loads sidecar and does not meet cold SLO.
2. Journal storage: actual token/reasoning producers already emit incremental deltas; append writer already uses O(1) cached sequence after initial scan. No second delta format or storage reduction was implemented. Cross-event references would require all replay/recovery readers and a new rollback decoder gate; current independent-row encoding retained.
3. Body/context versions: design reviewed but Agent-owned migration and all consumers are not implemented/pinned in this repository. Existing `ORDER BY id` replay cannot reuse old active tail IDs in correct post-summary order. No speculative content deduplication.
4. Sidecar retirement: field authority, atomic references and recovery/rollback parity not proven; duplicate writes remain deliberately enabled.
5. No 15-minute/24-hour sustained candidate acceptance or browser usability acceptance; candidate was rejected early. No historical disk savings claimed.

## Rollback readback

Restored image readback `sha256:a21968268925fb8582cc28d568d374ad97550c920e59347435b60126fcd9ce60`, healthy, OOMKilled=false, no active runs/streams. Authenticated rollback windows retained the same hashes/counts, both historical sidecar hashes unchanged; database still 499/461,491 and quick_check=ok, sidebar 484. `acceptance-next-rollback.json` retained.

Important qualification: the small-session delay persisted on rollback (0.700–2.422s), so it is **not proven to be caused by the candidate**. Candidate rejection is failure to establish the required end-to-end performance gate, not a proven candidate-specific regression. Existing service remains usable by authenticated API but latency diagnosis is open. Do not call all optimization complete.

Operational scripts/evidence remain outside source tree in the operator workspace. Production release evidence includes `acceptance-next-before.json`, `acceptance-next-after.json`, `acceptance-next-warm.json`, candidate and prior compose snapshots and `build-74a8c287.log`. Final rollback verification is recorded separately after readiness.
