# Deferred GET display resolution: contract reconciliation

## Outcome and scope

Audited at `bd9ff9e139150287cf5833290ed8ee1df0acaa59`. No production code change is required. The lone reported gate failure encoded the pre-`bd9ff9e1` display contract; that commit deliberately separated historical display identity from inference compatibility. This change replaces that stale assertion with behavior coverage, not a new runtime contract. No commit, deployment, or real session/configuration mutation was performed.

Contract routing: `AGENTS.md`, `docs/CONTRACTS.md`, `docs/GUIDELINES.md`; runtime ownership remains session sidecar/index plus agent state.db. Proposed run-state RFCs do not authorize a migration here.

## Authoritative call chain

- `GET /api/session` loads the session and applies profile visibility first. A session owned by another known profile returns **409 session_profile_mismatch**, not a rewritten model or silently accessible transcript.
- `resolve_model=1` calls `_resolve_effective_session_model_for_display` and `_resolve_effective_session_model_provider_for_display`. A nonempty historical model is returned verbatim. `openai/gpt-5.4-mini` is a slash-namespaced model, not explicit `@provider:model` provenance: a missing stored provider remains null.
- An empty historical model still uses `_read_profile_model_config` and the cache-only compatibility fallback. This guard is not a blanket ban on resolving defaults.
- `_handle_chat_start` independently reads the session's profile configuration and calls `_resolve_compatible_session_model_state` with profile provider/default/config. It passes the resolved pair to `_start_run`; `_prepare_chat_start_session_for_stream` persists that runnable pair for the new turn. Wakeup/stream paths also call the compatibility resolver; none was edited.
- The pinned example therefore displays `openai/gpt-5.4-mini` / null, but a plain send under its anthropic profile resolves and persists `claude-sonnet-4.6` / anthropic. A GET is not a request to execute or rewrite history.

## Tests and risks addressed

`tests/test_webui_state_db_reconciliation.py` replaces the stale test with a shared fixture and five collected cases:

1. Fast metadata GET (`messages=0&resolve_model=0`).
2. Deferred metadata GET (`messages=0&resolve_model=1`).
3. Full GET (`messages=1&resolve_model=1`).
4. Real chat-start handler and real prepare/persist helper, with only the runner boundary intercepted (no paid inference).
5. Empty-model profile fallback.

Each GET case checks the actual serialized response, forbids `Session.save`, and compares bytes and nanosecond mtimes of the sidecar, index, and private SQLite database. Switching the active-profile name away returns the existing 409; switching back preserves identity. This tests visibility outcomes, not the profile-switch HTTP endpoint itself. The historical model/provider on the original object also remain unchanged.

Risk: merely changing two expected strings could hide disabled inference repair. Countermeasure: real chat-start resolution and sidecar readback, plus a bypass negative control. Risk: allowing historical display could suppress missing-model defaults. Countermeasure: separate empty-model fallback case. Risk: a GET could save an unchanged value, silently touching metadata. Countermeasure: explicit save tripwire plus bytes/mtime checks and a save-injection negative control.

## Actual execution evidence

Local artifacts live under `/workspace/history-failure-evidence/deferred-display-resolution/`. Every run has `run.log`, `status.json` (actual subprocess return code), and `result.xml`. The runner `/workspace/history-failure-evidence/deferred-model-run.py` invokes the original `./scripts/test.sh`, scrubs credential variables, uses private HOME/HERMES_HOME/HERMES_BASE_HOME/config/WebUI/test state, and enables the test network blocker. Agent source is imported read-only from the existing checkout. No fabricated output was used.

| Run directory | Actual result | Meaning |
| --- | --- | --- |
| `red-original` | exit 1; 1 failed | Original node reproduces `openai/gpt-5.4-mini != claude-sonnet-4.6` on current code. |
| `green-focused` | exit 1; 3 failed, 2 passed | Preserved harness-development failure: initial test incorrectly expected cross-profile 200; corrected to existing 409 visibility contract, without changing routes. |
| `red-old-display` | exit 1; 2 failed, 1 passed | Process-local restoration of the two exact display functions from `bd9ff9e1^` makes both resolve_model=1 cases fail on historical identity; resolve_model=0 remains green. |
| `red-inference-bypass` | exit 1; 1 failed | Process-local compatibility no-op causes real chat-start runner arguments to retain the stale pair; the new inference test catches it. |
| `red-get-save` | exit 1; 1 failed | Injecting a save into display triggers `GET must not save a session`. |
| `green-focused-final` | exit 0; 5 passed | All added/revised behavior cases with unmodified runtime. |
| `green-ordered` | exit 0; 8 passed | Replays the three same-file predecessors found in original shard-08 XML, followed by the five new cases. |
| `green-neighbors` | exit 0; 165 passed | Full reconciliation file plus history display identity, provider mismatch, no-live-rebuild, issue5731 provider repair, and session-load model tests. |
| `green-neighbors-final` | exit 0; 165 passed | Final rerun after binding the loop variable explicitly for Ruff B023; Ruff and `git diff --check` also pass. |

Negative controls are supplied by `/workspace/history-failure-evidence/deferred_model_negative.py`; they modify only the test subprocess module namespace, not tracked production files. The old-display control is the exact prior implementation loaded via AST into the real route module, not a reimplementation of its expected output. This is historical-function differential evidence, not a claim to have run the entire prior commit. The ordered replay is same-file predecessor coverage, not the whole 16-shard gate. The parent owns that complete frozen gate.

No live provider request or generated assistant answer was tested: inference acceptance here ends at actual handler resolution, runner arguments, and real private sidecar persistence. Neighboring frontend source-shape tests still describe deferred hydration using older repair terminology; no frontend behavior change is claimed.

## Rollback

Revert only the test-file diff and this evidence note if the parent rejects the coverage. There is no runtime change or data migration to roll back. Do not revert `bd9ff9e1` merely to satisfy the old assertion: that would restore misleading historical display; the old-display negative control documents the resulting regression. Parent handles any eventual commit and deployment separately.
