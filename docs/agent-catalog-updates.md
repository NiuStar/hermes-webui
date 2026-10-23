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

## Availability diagnostics (independent WebUI / Agent results)

WebUI discovery reads bounded, same-repository published Release HTML sections,
not GitHub REST or the tag-only Atom feed. A current version absent from those
records stays `CURRENT_RELEASE_UNPUBLISHED`, with unknown direction and no WebUI
Apply. Publish the missing Release rather than weakening that gate. The UI
labels this warning as WebUI-only; it does not hide an install-ready Agent or
misreport a release-checked Docker snapshot as "not a Git checkout".

Agent availability is separate from upstream version discovery:
- `READY`: registry digest/platform and Compose preflight passed.
- `IMAGE_PENDING`: authoritative missing-manifest result; preparation progress
  is unknown, not assumed running.
- `IMAGE_PREPARING` / `IMAGE_FAILED`: only from recent matching builder evidence.
- `REGISTRY_UNAVAILABLE`: transport/auth/query failed; no absence inferred.
- `IMAGE_INCOMPATIBLE`, `RUNTIME_BLOCKED`, `CATALOG_DISABLED`,
  `UPDATER_UNAVAILABLE`, `PREFLIGHT_UNAVAILABLE`: explicit non-installable states.

For preparation evidence set `HERMES_AGENT_PREPARATION_STATUS_DIR` on the builder
to a **dedicated public diagnostic directory**, outside private output/receipts.
The builder writes atomic sanitized `agent-<webui>-<agent>.json` records (0644)
for preparing, failure and publication. Provide a read-only mirror of that
directory to the updater at `HERMES_WEBUI_AGENT_PREPARATION_DIR`; on separate
hosts the operator must arrange bounded file replication, not mount private
builder output or credentials. No mirror configured, invalid/future/expired or
wrong-target evidence remains `IMAGE_PENDING`/unknown. Preparing expires after
one hour; failure after one day. These records are **diagnostic only**: they
cannot enable installation or override registry/identity/Compose validation.
An actually ready registry image wins over old failure evidence. A network
failure remains unavailable even if a build receipt exists.

Non-ready Agent preflight results expire after 60 seconds rather than the normal
30-minute release cache; explicit Check now always forces a fresh request.
This change does not introduce unattended production installation or restarts.

## Builder lifecycle / unattended preparation

Enable `HERMES_AGENT_MANAGED_CLEANUP=1` only for the protected dedicated builder
output. The timer checks every five minutes under the existing single-flight
lock. Release HTML, archive and Git transport use up to three bounded attempts;
identity/ancestry refusal, build and tool-QA failures are never bypassed or
blindly retried. A published matching pair is label/platform-checked directly,
without repeatedly fetching its old Git history. Missing pairs still require
real Git ancestry before build and current Release revalidation before push.

Every attempt records its QA container ID and ownership label. After stopping,
the builder saves private metadata/logs and removes only that exact unmounted
QA container. Terminal receipts retain archive SHA, image identity, tool-loop
result and failure class; downloaded/expanded scratch is removed. Earlier
published attempts can be reclaimed only through their matching receipt and
exact stopped/no-mount container identity. Unreceipted historical QA is never
globally pruned. Cleanup errors preserve evidence and never hide the primary
QA failure. Abandoned running owners fail closed instead of being killed.

Only two latest receipted local catalog tags are retained, additionally keeping
the base/verifier and every image referenced by any container. Removal rechecks
exact local tag ID and never uses force, volume deletion or global Docker prune.
Legacy no-cache builds remove their intermediate containers instead of adding
per-release BuildKit cache; pin Docker's support for that builder and monitor
its deprecation. Registry artifacts remain available by immutable digest.

This prevents this builder's normal per-release scratch/QA accumulation, not
arbitrary growth by unrelated services on a shared disk. Below 3GiB after scoped
reclamation remains a real capacity failure and is surfaced via diagnostics.
New upstream runtime incompatibility also remains a genuine failure requiring
review; the system must not promise every future Release is compatible.

## Approved v0.22.5 dependency compatibility

Only official commit `bf0203a794a07aac3ff764972ab9087baa4e4f4e` / `v0.22.5`
has an operator-approved metadata correction: Pillow `13.0.0` -> `12.3.0`,
matching its pinned `browser-harness==0.1.13`. The original pyproject SHA-256
must equal `68325d6717bd4ebc3a7b015c5150bea4d5600c612259b9b7393e964caa828761`.
No Python source changes, dependency-check bypass, or future-release relaxation
is allowed. Preserve the upstream file privately and bake a
`.hermes-compatibility.json` recording both metadata hashes, commit and delta.
The release receipt records that same evidence and the actual runtime tests:
`uv pip check`, Pillow/browser-harness/pillow-heif imports, PNG/JPEG roundtrip,
normal init, tool loop and unchanged source/runtime gates. A failed gate prevents
publication; production Apply remains operator-controlled. This is a downstream
compatibility build of the official release, not an unmodified upstream image.
