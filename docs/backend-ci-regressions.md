# Backend CI regression repairs

## Contract routing

This repair preserves the existing approval-control and session-sidecar durability
contracts. It changes two backend implementation seams, not release policy:

- Approval polling imports the mirror predicate and retained-run TTL constants
  from their existing owner, `api.route_approvals`. A populated approval queue
  must not raise `NameError`; expired retained runs are stopped once, while
  unexpired requests remain pending.
- Legacy on-disk message counts parse the raw sidecar instead of calling
  self-healing `Session.load`. The previous path recursed through
  `Session.load -> save -> _persisted_message_count -> Session.load`, rewriting
  the original transcript before the shrink backup could be captured. The
  count reader now performs no session mutation, retains bounded signature
  retries, and populates the existing signature-bound facts cache. The dedupe
  regression requires the backup to equal the complete pre-shrink payload.

These affect the approval registry projection and sidecar/facts-cache layers.
They do not change visible transcript semantics, recency, recovery policy,
certificate validation, or production launch behavior.

## Existing tests repaired without weakening product safeguards

The affected files are byte-identical between baseline `e3d87824` and the
pre-repair candidate `6ca7be30`. These failures predate the release repair.

- Session deletion tests now exercise HTTP deletion and read back sidecar,
  backup, and index state. The old 2,400-character source window ended before
  the existing cleanup operations. The index test also preserves a second row.
- The journal pagination fixture explicitly identifies its fake session as a
  small sidecar; a missing real sidecar otherwise correctly triggers the
  fail-closed bounded-display path.
- Storage-health fixtures isolate maintenance's process-global last result.
- The launchd Git mock asserts the existing per-repository `safe.directory`
  arguments instead of treating those safety arguments as an unknown command.
- TLS health fixtures declare `Content-Length`, matching the real handler.
  The old close-delimited TLS fixture produced curl error 56 (`unexpected eof
  while reading`) despite delivering a body. No curl error is suppressed and
  no production TLS policy is changed.
- Completion previews accept either existing redactor's exact masked shape,
  retain plaintext-secret rejection, add escaped-secret rejection, and clear
  the optional public URL for the no-link control.
- Agent registry integration tests use explicit `importorskip` capability
  checks, consistent with the suite's optional-agent policy. A skipped registry
  integration is not verification of an installed agent.
- The old server line-count assertion is replaced by AST checks that every
  HTTP method still delegates to its corresponding `api.routes` handler.

## Verification scope

Run `./scripts/test.sh` with a supported Python 3.11–3.13 interpreter and
explicit owned `HERMES_WEBUI_TEST_STATE_DIR`, `HERMES_WEBUI_TEST_STATE_ROOT`, and
`TMPDIR` when other workers share the host. Imported environment values can
otherwise point independent worktrees at the same test runtime.

Focused backend and neighboring security/session suites completed with
408 passed and 19 optional-agent skips on Python 3.11. Whole-tree F821 checks
also passed. This is not a full-matrix or live-agent certification.
