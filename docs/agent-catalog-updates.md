# Agent page-update catalog

This path removes per-release `release.json` provisioning and separate publisher
keys. It does not remove normal runtime/Compose/active-run safety checks.

## One-time setup

1. Install the frozen `api/agent_image_catalog.py`,
   `api/official_agent_release.py`, `scripts/prepare_agent_release.py` and
   `scripts/agent_release_tool_probe.py` tree on the approved build host. Run
   `prepare_agent_release.py --base repository@sha256:<accepted-base> --root
   /protected/builder/output` without `--publish` for actual build/QA acceptance.
2. The builder follows the human-facing official Release HTML and verifies the
   peeled tag commit and ancestry over Git (no GitHub API). It preserves the
   exact WebUI file payload, builds only the Agent part, starts the normal init,
   runs the real fake-model -> terminal -> final-reply loop and full runtime
   source probe. Only a verified candidate can be pushed with `--publish`.
   Existing catalog tags are immutable: a repeat checks identities and returns
   `already_published`. A failed build/QA, source mismatch or changed Release
   prevents publication. Never describe a `--publish`-free run as published.
3. Configure the builder service/timer in `deploy/`. The base is tied to the
   accepted WebUI commit. Updating WebUI requires updating that one builder base;
   new Agent releases do NOT require per-version pins or editing the schedule.
   Do not enable the timer with insufficient space; there is a 3 GiB preflight
   but also retain headroom for the normal-init writable layer and audit output.
   If the accepted base predates a separately shipped updater correction, set
   `HERMES_AGENT_VERIFIER_IMAGE=repository@sha256:<accepted-updater>` in the
   builder's protected environment file. Its WebUI commit/version and platform
   must match the base. The candidate keeps the base WebUI payload, while its
   runtime acceptance runs through that immutable corrected updater. Publisher
   QA uses production `PYTHONPATH=/opt/hermes` and Agent directory, not a clean
   environment that can hide import-path failures. Deploy the frozen builder
   script update as well as the runtime updater; a Git push alone updates neither.
4. Use `docker-compose.agent-catalog.yml` on the target. The updater only has
   network-none/read-only root, the Docker socket, read-only Docker CLI/plugins,
   full ordered Compose config at matching host paths and a **dedicated image-
   override directory** writable for atomic pin/lock/rollback. Never grant it a
   whole production deployment/data directory writable. Configuration-only base
   files and `.env` stay read-only. Provision any other config referenced by the
   full stack read-only as well. Validate the effective stack, preserved UID,
   mounts, ports, project/service owner and image-only diff before manual start.
5. Set `HERMES_WEBUI_AGENT_CATALOG_ENABLED=1`, Compose project/service, and full
   absolute ordered files in `HERMES_WEBUI_AGENT_COMPOSE_FILES` (colon separated).
   The last file must contain a literal current digest for that service. The
   old root-tag-only Compose file is not sufficient. No `release.json` is needed.
   Restart remains operator-owned; these templates do not mutate production.

## Per-release flow

The WebUI independently discovers the official upstream Release and proves
ancestry. The sidecar derives `agent-<40-char-webui-sha>-<40-char-agent-sha>` in
its fixed repository, queries Docker Engine distribution metadata without
pulling or running containers, and offers installation only with a matching
platform digest and valid Compose capabilities. Browser payloads never select
an image/repository, target or file. If the builder has not finished QA/publishing,
installation stays unavailable; it does not require a human-created manifest.

Apply revalidates Release/ancestry through the WebUI, resolves the catalog tag
again to a digest, then the sidecar pulls **that digest**, validates all image
labels, unchanged WebUI identity and exact live owner, rechecks active tasks,
and executes only the target service transaction. After health, the real venv
and baked/staged Agent source are verified. Success pins the effective Compose
image; a failure rolls back exact YAML and recreates the old immutable image.
Restored container ID may differ. Sibling IDs must remain unchanged. Read status
to terminal state: an accepted operation is not a completed update.

This feature requires both the newly shipped WebUI and updater code. Publishing
an Agent-only image for an older WebUI does not magically retrofit catalog
support into its existing sidecar. Keep this one-time deployment distinct from
all future Agent updates. An empty-state isolated success is not production-state
migration acceptance.
