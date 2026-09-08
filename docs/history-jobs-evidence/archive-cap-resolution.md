# Archive member-cap timeout resolution

## Scope and root cause

Base: `bd9ff9e139150287cf5833290ed8ee1df0acaa59`. No commit or deployment performed.
Implementation owner: `api/upload.py::extract_archive`; `api/routes.py` imports upload handlers and was not edited. Both workspace upload and attachment archive extraction call this shared function.

The original ZIP and TAR loops checked `len(extracted_files) >= 10000` immediately before extracting the next file. Thus a valid 10001-file archive first created/wrote 10000 files using the security-preserving anchored filesystem helpers, then rejected and recursively cleaned them up. This is unnecessary filesystem work on a known-invalid input, not an insufficient client timeout. The original HTTP test reproduced `TimeoutError: timed out` at urllib socket read (unchanged `timeout=10`).

Ranked hypotheses considered: (1) late member-count rejection causes filesystem work; (2) archive compression/metadata parsing dominates; (3) server startup or request routing delays. The original HTTP failure plus deterministic ZIP/TAR first-member-create traps establish (1); metadata-only rejection makes the same HTTP test pass without changing transport limits or startup.

## Minimal repair and invariants

Before extracting, filter the already-loaded `ZipInfo` / `TarInfo` metadata using the same predicates as before, reject `len(members) > 10000`, then iterate that same list. ZIP directories remain excluded; TAR nonregular members remain excluded. Exactly 10000 files remain allowed. Actual-byte caps, path traversal checks, anchored mkdir/open, duplicate handling and failure cleanup are unchanged. The extraction root may be created before rejection, but no member file is created and the root is cleaned up.

State touched is exclusively the test-owned extraction directory and isolated test sessions/state. No real history database, backup, original repository tmp artifact, or deployment state was intentionally edited or deleted.

## Executed evidence

All runs used the existing, unmodified `/workspace/history-failure-evidence/restart_run.py`, which executes `./scripts/test.sh` with private HOME/Hermes homes and WebUI state. Wrapper `--timeout=60` was already present and unchanged; HTTP urllib remains `timeout=10`. The wrapper itself exits zero even when pytest fails: use its JSON **returncode** and JUnit, not the outer shell code.

Raw `.json`, `.xml`, `.log` files are retained under `/workspace/history-failure-evidence/restart-resolution/` with the following stems. Durations below are JUnit suite times (include fixture work), not HTTP latency.

| Stem | Internal pytest code | JUnit result | Suite seconds |
|---|---:|---|---:|
| archive-cap-baseline | 1 | 1 failed, original HTTP TimeoutError | 29.925 |
| archive-cap-red | 1 | 2 failed, ZIP/TAR attempted first member creation | 21.460 |
| archive-cap-green | 0 | 3 passed, same new tests plus original HTTP test | 21.759 |
| archive-cap-neighbors | 0 | 62 passed, no failures/errors/skips | 56.978 |
| archive-cap-negative | 1 | 2 failed after reverting only implementation patch | 21.570 |
| archive-cap-restored | 0 | 3 passed after restoring implementation patch | 21.875 |

RED ran before the implementation edit; GREEN ran immediately after it, before adding boundary checks. Negative control reverted only `api/upload.py` using the saved patch, left tests unchanged, observed both intended failures, restored the same patch, and reran the original HTTP case and regressions. Original HTTP testcase JUnit time is 2.645s in GREEN and 2.649s after restoration (not a standalone request benchmark).

Commands, with `R=/workspace/history-failure-evidence/restart_run.py`:

```sh
python "$R" archive-cap-baseline tests/test_workspace_upload.py::TestWorkspaceUploadArchive::test_archive_member_count_cap_trips
python "$R" archive-cap-red tests/test_workspace_upload.py::test_archive_member_cap_rejects_before_member_creation
python "$R" archive-cap-green tests/test_workspace_upload.py::test_archive_member_cap_rejects_before_member_creation tests/test_workspace_upload.py::TestWorkspaceUploadArchive::test_archive_member_count_cap_trips --basetemp=/workspace/history-failure-evidence/restart-resolution/archive-cap-green-tmp
python "$R" archive-cap-neighbors tests/test_workspace_upload.py tests/test_pr2520_extract_attachment_dir.py tests/test_chat_upload_attachment_paths.py tests/test_session_active_profile_authorization.py --basetemp=/workspace/history-failure-evidence/restart-resolution/archive-cap-neighbors-tmp
```

Negative/restored runs use the same respective regression/GREEN node lists and distinct `archive-cap-negative-tmp` / `archive-cap-restored-tmp` basetemps. The first baseline/RED runs used pytest's default newly allocated temporary directories; subsequent runs explicitly used unique evidence-owned basetemps to avoid default retention cleanup affecting prior runs. No manual deletion of existing tmp directories was performed.

Coverage: real ZIP/TAR 10001-file rejection before member creation; real extraction of 0/1/10000 files with a directory entry; original HTTP timeout regression; neighboring workspace uploads, byte bombs, corrupt archives, traversal, TAR suffix variants, attachment location and session authorization. The no-member-create tests intercept only the filesystem create boundary, not extraction logic or metadata parsing. Exact-limit boundary tests use real filesystem extraction, not mocked output.

## Risks, countermeasures, rollback

- Additional filtered metadata list costs O(n) references; parsers already materialized full metadata before this change. This does not introduce a new archive parser or second parsing pass.
- Over-limit archives with another invalid member now report count rejection before traversal/content errors. Both remain fail-closed; valid-input behavior is unchanged.
- This fix does not bound all archive metadata parsing work or expand the established cap to directory/special entries. Those would be separate security-contract changes, not timeout fixes.
- Timing depends on the filesystem. Deterministic no-member-creation assertions guard the root cause independently of timing; the original 10-second HTTP assertion still runs.
- Full repository gate was not rerun by this subagent; parent must run the combined gate with concurrent model/history changes. No production acceptance or deployment is claimed.
- Roll back only the archive implementation/test/doc hunks, not the entire worktree. Saved implementation patch: `/workspace/history-failure-evidence/restart-resolution/archive-cap-implementation.patch`; reverse-apply it only if the target still matches. Reverting restores the known timeout/late rejection behavior. Do not reset unrelated agent work.

Final `git diff --check` passed. Owned changed files: `api/upload.py`, `tests/test_workspace_upload.py`, and this document. Concurrent changes in other files and pre-existing `tmpz7959or8.cjs` were left untouched.
