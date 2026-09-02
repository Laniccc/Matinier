# Universal Media Assistant Plugin Framework Implementation Plan

> **For Codex:** REQUIRED SKILL: Use `executing-plans` to implement this plan task-by-task with review checkpoints.

**Goal:** Build a secure, runtime-installable, container-isolated plugin framework for future media assistants while preserving the existing caption and private-meeting-assistant paths.

**Architecture:** Add a generic MediaSession/Event sidecar that projects durable Final captions without touching the caption hot path. Add a plugin host inside the existing FastAPI modular monolith: signed local OCI packages, a Docker-backed sandbox port, bidirectional JSON-RPC, capability brokering, declarative UI, durable permissions/cursors/audit, and a Next.js management/rendering surface. Keep third-party plugin processes outside the API process and keep the existing meeting assistant behind a compatibility bridge.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, asyncio subprocesses, httpx, Ed25519 via `cryptography`, SQLite WAL, Docker-compatible OCI runtime, pytest, Next.js 16, React 19, TypeScript 6, Vitest.

**Approved design:** `docs/plans/2026-08-27-media-assistant-plugin-framework-design.md`

---

## Global constraints

- Work in a dedicated, healthy Git worktree. The current workspace has an unreadable/empty `.git` directory, so repair or recreate the worktree before executing commit steps.
- Do not call plugins synchronously from `WorkerCaptionRuntime`, `TranslationRuntime`, or the Final persistence methods.
- Keep `SegmentRecord` and `TranslationSegmentRecord` authoritative for formal transcript exports.
- All new schema changes are additive. Current Alembic head is `20260812_0022`.
- Default plugin networking is `none`. Never mount the Docker socket, project root, user home, `.env`, or database into a plugin container.
- Do not pass provider credentials or the database URL to plugin environment variables.
- External writes require idempotency and reconciliation. Unknown outcomes are not directly retryable.
- The framework phase ships only a diagnostic/conformance plugin, not a scenario assistant.
- Preserve current API behavior and all existing tests.

## Task 1: Freeze the host contracts and configuration

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/app/settings.py`
- Create: `backend/app/media/__init__.py`
- Create: `backend/app/media/contracts.py`
- Create: `backend/app/plugins/__init__.py`
- Create: `backend/app/plugins/contracts.py`
- Create: `backend/tests/test_media_contracts.py`
- Create: `backend/tests/test_plugin_contracts.py`

**Step 1: Write failing MediaEvent contract tests**

Cover:

- allowed event families and event-type syntax;
- positive `sequence` and `revision`;
- bounded JSON payload;
- required `logical_id` for revision-bearing events;
- UTC timestamps;
- no embedded binary or sensitive-looking payload keys;
- round-trip serialization.

Use this target shape:

```python
event = MediaEvent(
    schema_version=1,
    event_id="evt-1",
    session_id="media-1",
    sequence=1,
    event_type="transcript.final",
    media_time_ms=1000,
    duration_ms=500,
    logical_id="segment:1",
    revision=2,
    finality="final",
    source="host.transcription",
    payload={"text": "hello", "language": "en-US"},
    created_at=datetime.now(UTC),
)
```

**Step 2: Write failing plugin manifest and RPC envelope tests**

Define and test:

- `PluginId` syntax using reverse-domain identifiers;
- semantic version and host API range;
- permission, subscription, command, and resource-limit normalization;
- duplicate declarations rejected;
- JSON-RPC request/response/error envelopes with bounded IDs and payloads;
- `SessionScope` as an opaque host-issued value;
- stable error codes such as `plugin.protocol.invalid` and `plugin.permission.denied`.

**Step 3: Run the focused tests and verify failure**

Run from `backend`:

```powershell
uv run pytest tests/test_media_contracts.py tests/test_plugin_contracts.py -q
```

Expected: FAIL because `app.media.contracts` and `app.plugins.contracts` do not exist.

**Step 4: Implement the frozen Pydantic contracts**

Use `ConfigDict(extra="forbid", frozen=True)` for boundary models. Define at least:

```python
MediaMode = Literal["live", "playback"]
MediaTrackKind = Literal["audio", "video", "transcript", "chat"]
EventFinality = Literal["draft", "final", "derived"]
PluginLifecycleStatus = Literal[
    "installed", "disabled", "starting", "ready",
    "degraded", "crashed", "quarantined", "incompatible",
]
CapabilityEffect = Literal["read", "local_write", "network", "external_write"]
```

Keep version constants in one place:

```python
HOST_API_VERSION = "1.0.0"
MEDIA_SCHEMA_VERSION = 1
PLUGIN_RPC_VERSION = 1
UI_SCHEMA_VERSION = 1
CAPABILITY_API_VERSION = 1
```

**Step 5: Add settings with safe defaults**

Add fields for:

- `PLUGIN_FRAMEWORK_ENABLED`, default `true` in development;
- `PLUGIN_ALLOW_UNSIGNED`, default `false`;
- `PLUGIN_CONTAINER_RUNTIME`, initial literal `docker`;
- package, image, state, staging, and audit directories under `DATA_DIR/plugins`;
- package compressed/uncompressed limits;
- RPC line/message limits and timeouts;
- container memory, CPU, PID, tmpfs, crash-loop, and shutdown limits;
- `PLUGIN_ADMIN_TOKEN` as an optional secret required by mutating management APIs.

Extend `persistent_directories` so directories are created and checked centrally. Do not make API readiness fail merely because Docker is unavailable when no plugin is enabled.

**Step 6: Add the cryptographic dependency**

Add:

```toml
"cryptography>=45,<47",
```

Run:

```powershell
uv lock
uv sync
```

Expected: lock and environment update successfully.

**Step 7: Run tests**

```powershell
uv run pytest tests/test_media_contracts.py tests/test_plugin_contracts.py -q
```

Expected: PASS.

**Step 8: Commit**

```powershell
git add backend/pyproject.toml backend/uv.lock backend/app/settings.py backend/app/media backend/app/plugins backend/tests/test_media_contracts.py backend/tests/test_plugin_contracts.py
git commit -m "feat: define media plugin host contracts"
```

## Task 2: Add the MediaSession and MediaEvent persistence foundation

**Files:**
- Modify: `backend/app/persistence/models.py`
- Create: `backend/alembic/versions/20260827_0023_create_media_session_events.py`
- Create: `backend/app/media/repository.py`
- Create: `backend/tests/test_media_repository.py`
- Modify: `backend/tests/conftest.py` only if shared fixtures are needed

**Step 1: Write failing repository tests**

Test:

- creating one MediaSession bridge for one legacy Session;
- repeated bridge creation returns the same record;
- appending events allocates monotonically increasing per-session sequences;
- identical source event and revision are idempotent;
- a higher revision is accepted under the same logical identity;
- cursor listing is ordered and bounded;
- events from another session never leak;
- acknowledgement cannot move backwards.

**Step 2: Run the focused test**

```powershell
uv run pytest tests/test_media_repository.py -q
```

Expected: FAIL because the records and repository do not exist.

**Step 3: Add ORM records**

Add:

- `MediaSessionRecord`: ID, optional unique `legacy_session_id`, mode, source kind, status, owner scope placeholder, timestamps, next sequence.
- `MediaEventRecord`: event ID, MediaSession FK, sequence, type, media timing, logical ID, revision, finality, source, payload JSON, created time.
- `MediaBridgeOffsetRecord`: MediaSession, source table/type, source logical ID, processed revision and timestamp.
- `MediaConsumerCursorRecord`: MediaSession, consumer ID, last delivered and last acknowledged sequence.

Required uniqueness:

- `media_sessions.legacy_session_id`;
- `(media_session_id, sequence)`;
- `(media_session_id, source, logical_id, revision)`;
- `(media_session_id, consumer_id)`.

**Step 4: Create Alembic migration `0023`**

Set:

```python
revision = "20260827_0023"
down_revision = "20260812_0022"
```

Use SQLite-compatible constraints and indexes. The downgrade removes only the new tables.

**Step 5: Implement `MediaRepository`**

Expose transaction-scoped methods:

```python
ensure_legacy_session_bridge(...)
append_event(...)
list_events_after(...)
get_or_create_cursor(...)
acknowledge_cursor(...)
advance_bridge_offset(...)
```

Allocate the next sequence in the same write transaction as the event insert. Treat a uniqueness race as an idempotent lookup, not a duplicate event.

**Step 6: Verify migration and tests**

```powershell
uv run alembic upgrade head
uv run pytest tests/test_media_repository.py -q
uv run alembic downgrade 20260812_0022
uv run alembic upgrade head
```

Expected: migration round-trip succeeds and tests PASS.

**Step 7: Commit**

```powershell
git add backend/app/persistence/models.py backend/alembic/versions/20260827_0023_create_media_session_events.py backend/app/media/repository.py backend/tests/test_media_repository.py
git commit -m "feat: persist media sessions and events"
```

## Task 3: Project existing durable captions into MediaEvents

**Files:**
- Create: `backend/app/media/projector.py`
- Create: `backend/app/media/bootstrap.py`
- Create: `backend/tests/test_media_event_projector.py`
- Modify: `backend/app/main.py` later only after Task 10; keep this task isolated

**Step 1: Write failing projector tests**

Seed a legacy Session with:

- two Final `SegmentRecord` rows;
- one Final `TranslationSegmentRecord`;
- a terminal Session state.

Verify the projector emits:

- `transcript.final` with text, language, track, evidence identity, media times, and source revision;
- `translation.final` with source segment IDs and target language;
- `session.completed` once;
- no event for Partial records;
- a higher Final revision as a new event under the same logical ID;
- no duplicates after a second scan;
- no synchronous model or plugin call.

**Step 2: Run the focused test**

```powershell
uv run pytest tests/test_media_event_projector.py -q
```

Expected: FAIL because `MediaEventProjector` is missing.

**Step 3: Implement the sidecar projector**

Follow the existing `MeetingStateProjector` ownership pattern, but keep extraction deterministic and model-free. Poll durable tables, batch by Session, advance offsets only after the MediaEvent transaction commits, and expose:

```python
class MediaEventProjector:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def request_catch_up(self, legacy_session_id: str) -> MediaCatchUpResult: ...
```

Do not import plugin supervisor code from the projector.

**Step 4: Add bootstrap configuration**

Build projector limits from Settings. The object is startable independently for tests and can later be composed into `PluginHostRuntime`.

**Step 5: Run tests**

```powershell
uv run pytest tests/test_media_event_projector.py tests/test_caption_runtime.py tests/test_translation_runtime.py -q
```

Expected: PASS; existing caption and translation runtime tests remain unchanged.

**Step 6: Commit**

```powershell
git add backend/app/media/projector.py backend/app/media/bootstrap.py backend/tests/test_media_event_projector.py
git commit -m "feat: project final captions into media events"
```

## Task 4: Add plugin package, publisher trust, and installation persistence

**Files:**
- Modify: `backend/app/persistence/models.py`
- Create: `backend/alembic/versions/20260827_0024_create_plugin_platform.py`
- Create: `backend/app/plugins/repository.py`
- Create: `backend/tests/test_plugin_repository.py`

**Step 1: Write failing persistence tests**

Cover:

- multiple immutable versions per plugin ID;
- exactly one preferred version;
- publisher-key fingerprint pinning;
- staged package ticket expiry and one-time consumption;
- base permissions by plugin/version;
- runtime status and crash counters;
- MediaSession bindings pinned to a plugin version;
- cursor persistence;
- namespaced state with optimistic versioning and quota accounting;
- UI view versioning;
- capability grants, invocations, and audit events.

**Step 2: Run the focused test**

```powershell
uv run pytest tests/test_plugin_repository.py -q
```

Expected: FAIL.

**Step 3: Add plugin ORM records**

Create tables for:

- `plugin_publishers`;
- `plugin_packages`;
- `plugin_installations`;
- `plugin_staging_tickets`;
- `plugin_permissions`;
- `plugin_runtime_health`;
- `plugin_session_bindings`;
- `plugin_state_items`;
- `plugin_ui_views`;
- `plugin_capability_grants`;
- `plugin_capability_invocations`;
- `plugin_audit_events`.

Use JSON only for bounded typed payloads; keep identity, status, version, capability, effect, expiry, cursor, and idempotency columns queryable.

**Step 4: Create migration `0024`**

```python
revision = "20260827_0024"
down_revision = "20260827_0023"
```

Add indexes for plugin ID/version, active installation, binding state, expiry scans, audit time, and invocation idempotency.

**Step 5: Implement `PluginRepository`**

Repository methods must require explicit plugin ID/version and MediaSession scope. State writes use an expected version. Capability-invocation creation is idempotent on `(plugin_id, idempotency_key)`.

**Step 6: Verify**

```powershell
uv run alembic upgrade head
uv run pytest tests/test_plugin_repository.py -q
```

Expected: PASS.

**Step 7: Commit**

```powershell
git add backend/app/persistence/models.py backend/alembic/versions/20260827_0024_create_plugin_platform.py backend/app/plugins/repository.py backend/tests/test_plugin_repository.py
git commit -m "feat: persist plugin installation and runtime state"
```

## Task 5: Implement secure package inspection and installation

**Files:**
- Create: `backend/app/plugins/manifest.py`
- Create: `backend/app/plugins/signing.py`
- Create: `backend/app/plugins/package_store.py`
- Create: `backend/tests/test_plugin_package_store.py`
- Create: `backend/tests/fixtures/plugin_packages/.gitkeep`

**Step 1: Write archive-attack and signature tests**

Generate temporary ZIPs in tests and cover:

- valid signed package;
- tampered manifest, image, assets, or signature;
- unsigned package rejected unless development override is explicit;
- unknown publisher requires an explicit trust operation;
- path traversal, absolute paths, drive paths, symlinks, duplicate entries;
- too many entries, excessive compressed/uncompressed size, and compression ratio;
- image digest mismatch;
- incompatible Host API;
- immutable content-addressed destination.

**Step 2: Run the focused test**

```powershell
uv run pytest tests/test_plugin_package_store.py -q
```

Expected: FAIL.

**Step 3: Implement canonical signing**

Use Ed25519. Canonical signed material must include:

```text
canonical(plugin.json)
newline
image_digest
newline
assets_digest
```

Never trust a public key merely because it is inside the same package. Return its fingerprint for explicit pinning or compare it to an already trusted publisher record.

**Step 4: Implement safe ZIP inspection**

Read central-directory metadata before extracting. Normalize every path, reject links and duplicates, enforce entry and byte limits, and extract only to a newly created staging directory under the configured plugin staging root.

Return a durable one-time inspection ticket containing the verified manifest hash, staged content path, permission request, signature status, and expiry.

**Step 5: Implement confirmed install**

On confirmation:

- revalidate ticket and staged hashes;
- pin an explicitly approved publisher key if required;
- copy package content to `packages/<plugin-id>/<version>/<content-digest>`;
- import the OCI image through the `ContainerRuntime` port added in Task 7;
- record the immutable package and disabled installation;
- consume the ticket;
- clean staging files in `finally`.

Keep image import behind a protocol so this task can use a fake implementation until Task 7.

**Step 6: Run tests**

```powershell
uv run pytest tests/test_plugin_package_store.py -q
```

Expected: PASS.

**Step 7: Commit**

```powershell
git add backend/app/plugins/manifest.py backend/app/plugins/signing.py backend/app/plugins/package_store.py backend/tests/test_plugin_package_store.py backend/tests/fixtures/plugin_packages/.gitkeep
git commit -m "feat: securely inspect and install plugin packages"
```

## Task 6: Implement bounded bidirectional JSON-RPC

**Files:**
- Create: `backend/app/plugins/rpc.py`
- Create: `backend/tests/test_plugin_rpc.py`

**Step 1: Write failing protocol tests**

Use in-memory `asyncio.StreamReader`/writer doubles. Cover:

- request/response correlation;
- plugin-to-host method dispatch while host requests are pending;
- bounded line and decoded message sizes;
- malformed JSON and invalid JSON-RPC envelopes;
- duplicate request ID behavior;
- deadlines and cancellation;
- EOF with pending requests;
- stderr kept outside protocol framing;
- graceful shutdown;
- stable public error conversion without raw internal exceptions.

**Step 2: Run the test**

```powershell
uv run pytest tests/test_plugin_rpc.py -q
```

Expected: FAIL.

**Step 3: Implement `JsonRpcPeer`**

Provide:

```python
class JsonRpcPeer:
    def register_handler(self, method: str, handler: RpcHandler) -> None: ...
    async def start(self) -> None: ...
    async def request(self, method: str, params: BaseModel, *, timeout: float) -> object: ...
    async def notify(self, method: str, params: BaseModel) -> None: ...
    async def aclose(self) -> None: ...
```

Use a single read loop, a serialized write lock, bounded pending calls, and cancellation-safe cleanup. Notifications cannot invoke write capabilities.

**Step 4: Run tests**

```powershell
uv run pytest tests/test_plugin_rpc.py -q
```

Expected: PASS.

**Step 5: Commit**

```powershell
git add backend/app/plugins/rpc.py backend/tests/test_plugin_rpc.py
git commit -m "feat: add bounded plugin json rpc transport"
```

## Task 7: Implement the Docker sandbox runtime and supervisor

**Files:**
- Create: `backend/app/plugins/container_runtime.py`
- Create: `backend/app/plugins/supervisor.py`
- Create: `backend/tests/test_plugin_container_runtime.py`
- Create: `backend/tests/test_plugin_supervisor.py`
- Create: `backend/smoke/plugin_sandbox_smoke.py`

**Step 1: Write failing Docker command-construction tests**

Assert the exact immutable argument list contains:

```text
docker run --rm -i
--network none
--read-only
--cap-drop ALL
--security-opt no-new-privileges
--pids-limit <bounded>
--memory <bounded>
--cpus <bounded>
--tmpfs /tmp:rw,noexec,nosuid,size=<bounded>
```

Assert it does not contain:

- Docker socket mounts;
- project, home, database, or `.env` paths;
- host network;
- shell command strings;
- inherited environment secrets.

**Step 2: Write failing supervisor tests**

Use `FakeContainerRuntime` to verify:

- one process per enabled plugin version;
- concurrent `session.open` calls create logical bindings;
- heartbeat transitions `starting -> ready -> degraded`;
- unexpected exit transitions to `crashed`;
- bounded exponential restart;
- crash-loop threshold enters `quarantined`;
- shutdown drains sessions then terminates;
- one plugin crash does not stop another;
- stale RPC scopes are rejected after restart.

**Step 3: Run tests**

```powershell
uv run pytest tests/test_plugin_container_runtime.py tests/test_plugin_supervisor.py -q
```

Expected: FAIL.

**Step 4: Implement the runtime port**

Define:

```python
class ContainerRuntime(Protocol):
    async def available(self) -> bool: ...
    async def import_image(self, image_tar: Path, expected_digest: str) -> ImportedImage: ...
    async def start(self, spec: PluginContainerSpec) -> PluginContainerProcess: ...
    async def stop(self, container_id: str, *, timeout: float) -> None: ...
```

The Docker adapter must use `asyncio.create_subprocess_exec` with a fixed argument array and sanitized environment. Capture protocol stdout, plugin log stderr, and exit code separately.

**Step 5: Implement `PluginSupervisor`**

Supervisor responsibilities:

- start preferred enabled versions;
- negotiate protocol and Host API;
- register host RPC handlers;
- restore MediaSession bindings and cursors;
- heartbeat and resource-state persistence;
- restart/quarantine policy;
- graceful disable and application shutdown.

Do not let shutdown wait forever on a plugin.

**Step 6: Add opt-in real sandbox smoke**

The smoke fixture must attempt to:

- read an unmounted host sentinel;
- read a fake secret from the host environment;
- resolve or fetch a public URL;
- write outside its private data mount.

Expected: all attempts fail, while JSON-RPC and private-state writes succeed.

Mark it opt-in so regular pytest does not require Docker.

**Step 7: Run unit tests**

```powershell
uv run pytest tests/test_plugin_container_runtime.py tests/test_plugin_supervisor.py -q
```

Expected: PASS.

**Step 8: Run opt-in smoke**

```powershell
uv run python smoke/plugin_sandbox_smoke.py
```

Expected: `sandbox_ok=true` and no leaked sentinel/secret/network access.

**Step 9: Commit**

```powershell
git add backend/app/plugins/container_runtime.py backend/app/plugins/supervisor.py backend/tests/test_plugin_container_runtime.py backend/tests/test_plugin_supervisor.py backend/smoke/plugin_sandbox_smoke.py
git commit -m "feat: supervise plugins in docker sandboxes"
```

## Task 8: Add permissions, grants, state, and Capability Broker

**Files:**
- Create: `backend/app/plugins/permissions.py`
- Create: `backend/app/plugins/capabilities.py`
- Create: `backend/app/plugins/broker.py`
- Create: `backend/tests/test_plugin_permissions.py`
- Create: `backend/tests/test_plugin_broker.py`

**Step 1: Write failing authorization tests**

Cover:

- base permission accepted at install;
- session-only sensitive-media permission;
- denial by default;
- plugin/version/session mismatch;
- expired and revoked grants;
- destination/method/call-count/scope expansion;
- state namespace isolation and quota;
- no secret fields in broker output;
- external-write tools require idempotency and reconciliation;
- unknown outcome cannot directly retry.

**Step 2: Write failing broker tests**

Use fake model, network, state, playback, and external-action adapters. Test:

- `media.query` reads only allowed MediaSession events;
- `model.invoke` enforces input category, token, timeout, and concurrency budgets;
- `network.fetch` validates HTTPS, public destination, redirects, response MIME and size;
- `state.get/put` uses plugin namespace and optimistic version;
- `ui.publish` delegates to validated UI persistence from Task 9;
- `action.execute` records invocation before the side effect and reconciles uncertain results;
- every call appends an audit record.

**Step 3: Run tests**

```powershell
uv run pytest tests/test_plugin_permissions.py tests/test_plugin_broker.py -q
```

Expected: FAIL.

**Step 4: Implement `PermissionEvaluator`**

Reuse the structural scope-subset behavior from `app.assistant.grants` where safe, but keep plugin grants in their own domain. Authorization must be pure and deterministic.

**Step 5: Implement `CapabilityRegistry`**

Use a typed spec:

```python
@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    name: str
    version: str
    effect: CapabilityEffect
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    timeout_seconds: float
    requires_action_grant: bool
    supports_idempotency: bool
    supports_reconciliation: bool
```

Register only host-owned adapters. Plugins can request a capability but cannot register or replace host capabilities.

**Step 6: Implement broker RPC handlers**

Translate plugin JSON-RPC into typed invocations, derive identity from the connected container instead of trusting `plugin_id` in parameters, validate scope, persist invocation, execute, sanitize output, and persist completion atomically where possible.

**Step 7: Run tests**

```powershell
uv run pytest tests/test_plugin_permissions.py tests/test_plugin_broker.py tests/test_private_meeting_agent_core.py -q
```

Expected: PASS.

**Step 8: Commit**

```powershell
git add backend/app/plugins/permissions.py backend/app/plugins/capabilities.py backend/app/plugins/broker.py backend/tests/test_plugin_permissions.py backend/tests/test_plugin_broker.py
git commit -m "feat: broker scoped plugin capabilities"
```

## Task 9: Validate and persist declarative plugin UI

**Files:**
- Create: `backend/app/plugins/ui_schema.py`
- Create: `backend/tests/test_plugin_ui_schema.py`
- Create: `frontend/types/plugin-ui.ts`
- Create: `frontend/lib/plugin-ui-schema.ts`
- Create: `frontend/lib/plugin-ui-schema.test.ts`

**Step 1: Write failing backend UI tests**

Test:

- allowed surfaces and components;
- maximum depth, nodes, strings, table rows, options, and actions;
- `SafeMarkdown` rejects raw HTML and unsafe links;
- no script, style, iframe, arbitrary URL, or permission-prompt component;
- overlay component allowlist is smaller than panel allowlist;
- view version must increase;
- action and media-anchor validation;
- invalid view never replaces the previous valid view.

**Step 2: Implement backend schema**

Use a discriminated Pydantic union for:

```text
text, safe_markdown, card, section, tabs, list, table,
timeline, media_anchor, badge, metric, progress,
input, textarea, select, checkbox, button,
confirmation, empty_state, error_state
```

Persist a validated canonical JSON view keyed by plugin/version/session/surface/view ID.

**Step 3: Run backend tests**

```powershell
uv run pytest tests/test_plugin_ui_schema.py -q
```

Expected: PASS.

**Step 4: Write failing frontend parser tests**

Use hostile JSON fixtures for:

- unknown components;
- unsafe links;
- oversized/nested trees;
- missing stable IDs;
- stale versions;
- fake permission prompts.

Run:

```powershell
pnpm test -- lib/plugin-ui-schema.test.ts
```

Expected: FAIL because parser is missing.

**Step 5: Implement the TypeScript boundary parser**

Do not cast API JSON directly to render types. Parse unknown JSON into a closed discriminated union and return a safe error view on failure.

**Step 6: Run frontend tests**

```powershell
pnpm test -- lib/plugin-ui-schema.test.ts
pnpm typecheck
```

Expected: PASS.

**Step 7: Commit**

```powershell
git add backend/app/plugins/ui_schema.py backend/tests/test_plugin_ui_schema.py frontend/types/plugin-ui.ts frontend/lib/plugin-ui-schema.ts frontend/lib/plugin-ui-schema.test.ts
git commit -m "feat: validate declarative plugin views"
```

## Task 10: Compose PluginHostRuntime and expose management/media APIs

**Files:**
- Create: `backend/app/plugins/bootstrap.py`
- Create: `backend/app/api/plugins.py`
- Create: `backend/app/api/media.py`
- Modify: `backend/app/api/dependencies.py`
- Modify: `backend/app/api/health.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/test_plugins_api.py`
- Create: `backend/tests/test_media_api.py`
- Modify: `backend/tests/test_health.py`

**Step 1: Write failing API tests**

Management API:

```text
POST   /api/plugins/packages:inspect
POST   /api/plugins/installations
GET    /api/plugins
GET    /api/plugins/{plugin_id}
POST   /api/plugins/{plugin_id}/enable
POST   /api/plugins/{plugin_id}/disable
POST   /api/plugins/{plugin_id}/grants
DELETE /api/plugins/{plugin_id}
```

Media/plugin API:

```text
GET  /api/sessions/{session_id}/media-session
GET  /api/media-sessions/{media_session_id}/events?after=
GET  /api/media-sessions/{media_session_id}/plugin-views
POST /api/media-sessions/{media_session_id}/plugin-commands
```

Verify mutating management calls require `PLUGIN_ADMIN_TOKEN`, raw package upload is streamed and bounded, install is two-phase, APIs return safe errors, and one plugin cannot address another binding.

**Step 2: Run API tests**

```powershell
uv run pytest tests/test_plugins_api.py tests/test_media_api.py tests/test_health.py -q
```

Expected: FAIL.

**Step 3: Compose `PluginHostRuntime`**

Own:

- MediaEventProjector;
- PackageStore;
- CapabilityRegistry/Broker;
- PluginSupervisor;
- cleanup of expired staging tickets and grants.

Startup order:

```text
database ready
-> media projector
-> plugin supervisor recovery
-> API accepts plugin operations
```

Shutdown order is the reverse. If no plugin is enabled, unavailable Docker is reported but does not make captions unavailable.

**Step 4: Add FastAPI dependency injection**

Add `plugin_host_runtime` injection hooks to `create_app` and `application.state`, mirroring `assistant_runtime`. Keep fake runtime injection possible for API tests.

**Step 5: Implement admin-token dependency**

Derive authority server-side. Never accept an actor or plugin identity merely from request JSON. Compare tokens in constant time and return 401/403 without logging the secret.

**Step 6: Extend readiness**

Add a separate plugin health object containing:

- framework enabled;
- container runtime availability;
- installed/enabled/ready/quarantined counts;
- media-projector state and lag;
- RPC queue/backlog summary.

Do not merge plugin degradation into caption readiness unless startup cannot preserve the caption path.

**Step 7: Run tests**

```powershell
uv run pytest tests/test_plugins_api.py tests/test_media_api.py tests/test_health.py tests/test_api_cold_import.py -q
```

Expected: PASS.

**Step 8: Commit**

```powershell
git add backend/app/plugins/bootstrap.py backend/app/api/plugins.py backend/app/api/media.py backend/app/api/dependencies.py backend/app/api/health.py backend/app/main.py backend/tests/test_plugins_api.py backend/tests/test_media_api.py backend/tests/test_health.py
git commit -m "feat: expose media plugin host api"
```

## Task 11: Build the plugin manager and declarative surface renderer

**Files:**
- Create: `frontend/types/plugins.ts`
- Modify: `frontend/lib/api.ts`
- Create: `frontend/lib/plugin-manager-state.ts`
- Create: `frontend/lib/plugin-manager-state.test.ts`
- Create: `frontend/components/plugins/plugin-manager.tsx`
- Create: `frontend/components/plugins/plugin-surface.tsx`
- Create: `frontend/components/plugins/plugin-component.tsx`
- Create: `frontend/app/plugins/page.tsx`
- Modify: `frontend/components/room-studio.tsx`
- Modify: `frontend/app/globals.css`

**Step 1: Write failing state-machine tests**

Test:

- package selected -> inspecting -> permission review -> installing -> disabled;
- enable/disable/update states;
- admin token kept out of persisted state;
- stale API responses ignored;
- unsafe/unknown views become a safe error card;
- plugin command includes expected view version and MediaSession scope;
- authorization-required response opens only the host-owned permission dialog.

Run:

```powershell
pnpm test -- lib/plugin-manager-state.test.ts
```

Expected: FAIL.

**Step 2: Add typed API clients**

The package inspection client must send the raw ZIP with the plugin media type, not JSON. Do not force `Content-Type: application/json` through the shared request helper.

Add functions for inspection, install confirmation, list/detail, enable/disable/uninstall, grants, MediaSession bridge, plugin views, and commands.

Keep the admin token only in component memory. Never place it in localStorage, query parameters, logs, or error text.

**Step 3: Implement the management page**

The page must show:

- package identity, publisher fingerprint, version, compatibility, and signature status;
- requested permissions grouped by base, sensitive media, network, and external write;
- unsigned-development warning;
- installed versions and preferred version;
- enable/disable/update/uninstall actions;
- runtime health and quarantine reason.

Use host-owned confirmation for install, trust, disable, and uninstall.

**Step 4: Implement recursive declarative rendering**

Render the closed component union exhaustively. Never use `dangerouslySetInnerHTML`. Open external links only through a host confirmation path. Media anchors call host playback control, not arbitrary plugin JavaScript.

**Step 5: Integrate plugin surfaces without enlarging meeting-specific coupling**

Add a small `PluginSurface` host beside `PrivateMeetingAssistant`. Resolve the active caption Session to a MediaSession through the API. Do not move meeting-specific candidate or Linear behavior into the plugin renderer.

**Step 6: Run frontend verification**

```powershell
pnpm test
pnpm typecheck
pnpm build
```

Expected: all tests pass, TypeScript exits 0, and Next.js production build succeeds with the new `/plugins` route.

**Step 7: Commit**

```powershell
git add frontend/types/plugins.ts frontend/lib/api.ts frontend/lib/plugin-manager-state.ts frontend/lib/plugin-manager-state.test.ts frontend/components/plugins frontend/app/plugins/page.tsx frontend/components/room-studio.tsx frontend/app/globals.css
git commit -m "feat: add plugin management and safe ui surfaces"
```

## Task 12: Publish the SDK schemas and diagnostic conformance plugin

**Files:**
- Create: `plugin-sdk/README.md`
- Create: `plugin-sdk/schema/plugin-manifest-v1.json`
- Create: `plugin-sdk/schema/media-event-v1.json`
- Create: `plugin-sdk/schema/plugin-rpc-v1.json`
- Create: `plugin-sdk/schema/ui-view-v1.json`
- Create: `plugin-sdk/examples/diagnostic/Dockerfile`
- Create: `plugin-sdk/examples/diagnostic/plugin.json`
- Create: `plugin-sdk/examples/diagnostic/plugin.py`
- Create: `plugin-sdk/examples/diagnostic/README.md`
- Create: `backend/scripts/package_diagnostic_plugin.py`
- Create: `backend/tests/test_plugin_sdk_schemas.py`
- Create: `backend/tests/test_diagnostic_plugin_contract.py`

**Step 1: Write failing schema parity tests**

Verify every Pydantic boundary example validates against the published JSON Schema and every invalid fixture fails both implementations. Verify schema version constants match.

**Step 2: Write failing diagnostic protocol test**

Launch the diagnostic implementation against in-memory streams and verify:

- initialize and capability negotiation;
- open two logical MediaSessions;
- receive and acknowledge Final events;
- read/write private state;
- publish a panel and timeline marker;
- handle a denied network request;
- survive a simulated reconnect;
- close sessions and shutdown.

**Step 3: Run tests**

```powershell
uv run pytest tests/test_plugin_sdk_schemas.py tests/test_diagnostic_plugin_contract.py -q
```

Expected: FAIL.

**Step 4: Publish generated schemas**

Generate JSON Schema from the authoritative Pydantic models, normalize ordering, and check committed schema files for drift in tests. Do not maintain separate hand-written semantics.

**Step 5: Implement diagnostic plugin using only the Python standard library**

The example is a protocol/conformance fixture, not a content assistant. It must not call external APIs or bundle secrets.

**Step 6: Implement the package script**

The script:

- builds the OCI image;
- exports `image.tar`;
- calculates deterministic digests;
- canonicalizes `plugin.json`;
- signs with an explicitly supplied development key;
- writes the local `.plugin.zip` outside tracked source.

Never generate or commit a private key.

**Step 7: Run tests and local package smoke**

```powershell
uv run pytest tests/test_plugin_sdk_schemas.py tests/test_diagnostic_plugin_contract.py -q
uv run python scripts/package_diagnostic_plugin.py --help
```

Expected: tests PASS and help exits 0 without building or writing.

**Step 8: Commit**

```powershell
git add plugin-sdk backend/scripts/package_diagnostic_plugin.py backend/tests/test_plugin_sdk_schemas.py backend/tests/test_diagnostic_plugin_contract.py
git commit -m "feat: publish plugin sdk and diagnostic plugin"
```

## Task 13: Prove update, recovery, and compatibility behavior end to end

**Files:**
- Create: `backend/tests/test_plugin_framework_e2e.py`
- Create: `backend/smoke/plugin_framework_smoke.py`
- Modify: `backend/tests/test_private_meeting_agent_core.py` only for an explicit compatibility assertion if needed
- Modify: `frontend/lib/live-caption-view.test.ts` only if integration exposes a regression

**Step 1: Write the end-to-end test with fakes**

Use a temporary SQLite database, FakeContainerRuntime, and FastAPI TestClient:

1. Seed an existing caption Session and Final rows.
2. Inspect and install a signed diagnostic package.
3. Accept base permissions and enable it.
4. Project MediaEvents and open the plugin binding.
5. Deliver events, acknowledge a cursor, and publish UI.
6. Crash the fake container and recover from the durable cursor.
7. Stage a new plugin version and keep the active session pinned.
8. Fail state migration and verify rollback.
9. Revoke permission and verify the next invocation fails closed.
10. Confirm the private meeting assistant can still build context from the same Session.

**Step 2: Run the test and verify failure**

```powershell
uv run pytest tests/test_plugin_framework_e2e.py -q
```

Expected: FAIL until all integration gaps are closed.

**Step 3: Make minimal integration fixes**

Do not broaden scopes or bypass the broker to make the test pass. Fix ownership, lifecycle, or transaction boundaries at their responsible component.

**Step 4: Add the real local smoke**

The smoke uses Docker and the diagnostic package to validate install, enable, Final-event delivery, UI publication, denied direct network, container restart, cursor resume, disable, and clean shutdown.

**Step 5: Run focused and full backend verification**

```powershell
uv run pytest tests/test_plugin_framework_e2e.py tests/test_private_meeting_agent_core.py tests/test_caption_runtime.py tests/test_translation_runtime.py -q
uv run pytest -q
```

Expected: focused suite and full suite PASS, with only existing documented skips.

**Step 6: Run opt-in real smoke**

```powershell
uv run python smoke/plugin_framework_smoke.py
```

Expected: JSON report with `plugin_ready=true`, `cursor_resumed=true`, `network_isolated=true`, and `caption_regression=false`.

**Step 7: Commit**

```powershell
git add backend/tests/test_plugin_framework_e2e.py backend/smoke/plugin_framework_smoke.py backend/tests/test_private_meeting_agent_core.py frontend/lib/live-caption-view.test.ts
git commit -m "test: verify plugin framework recovery and compatibility"
```

## Task 14: Complete operations, security documentation, and release checks

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/api-events.md`
- Create: `docs/plugin-authoring.md`
- Create: `docs/plugin-operations.md`
- Create: `docs/plugin-security.md`
- Modify: `.env.example`

**Step 1: Document the architecture**

Add:

- MediaSession/Event sidecar and plugin container diagram;
- trust boundaries and capability flow;
- package/signature format;
- lifecycle and JSON-RPC methods;
- permission/grant model;
- declarative UI schema;
- update, rollback, quarantine, and uninstall behavior;
- single-instance SQLite limitation.

**Step 2: Document authoring**

Include a minimal manifest, protocol handshake, event subscription, state, UI publication, commands, compatibility policy, packaging, signing, and conformance commands. State clearly that plugin stdout is protocol-only and logs go to stderr.

**Step 3: Document operations and security**

Include:

- Docker prerequisites;
- publisher-key trust and fingerprint review;
- admin-token setup;
- unsigned development mode warning;
- resource defaults;
- health interpretation;
- crash-loop recovery;
- permission revocation;
- audit review;
- rollback and recoverable uninstall;
- threat model and known limitations.

**Step 4: Update environment example**

Add safe placeholders and comments. Do not place a real admin token or signing key in the repository.

**Step 5: Run migration verification on a temporary database**

```powershell
$env:DATABASE_URL='sqlite:///./data/plugin-plan-verification.db'
uv run alembic upgrade head
uv run alembic current
```

Expected: current revision is `20260827_0024`. Remove the explicitly named temporary database and WAL/SHM sidecars after verifying their resolved paths are inside `backend/data`.

**Step 6: Run complete verification**

Backend:

```powershell
uv run python -m compileall app
uv run pytest -q
```

Frontend:

```powershell
pnpm test
pnpm typecheck
pnpm build
```

Runtime:

```powershell
uv run python smoke/plugin_sandbox_smoke.py
uv run python smoke/plugin_framework_smoke.py
```

Expected:

- Python compile exits 0;
- backend and frontend tests pass;
- TypeScript exits 0;
- Next.js production build succeeds;
- both Docker smoke reports pass;
- `GET /health/ready` reports caption readiness independently and reports plugin runtime details;
- the existing caption and private meeting assistant manual paths remain usable.

**Step 7: Review scope and security**

Confirm:

- no scenario-specific assistant was added;
- no plugin code runs in the API process;
- no plugin receives secrets, raw database access, project mounts, or direct network;
- no arbitrary plugin HTML/JS/CSS is rendered;
- no external write can execute without a short-lived scoped grant;
- the README does not claim multi-tenant, horizontal-scale, or production-grade sandbox support beyond the tested Docker boundary.

**Step 8: Commit**

```powershell
git add README.md docs/architecture.md docs/api-events.md docs/plugin-authoring.md docs/plugin-operations.md docs/plugin-security.md .env.example
git commit -m "docs: document the media plugin framework"
```

## Final review checkpoint

Before merging or beginning scenario-plugin work:

1. Review both Alembic migrations against a production-like SQLite backup.
2. Inspect every container argument and mounted path in the real smoke report.
3. Inspect every capability and permission exposed in the Host API.
4. Confirm API identity is derived from the authenticated/admin connection, never plugin JSON.
5. Confirm MediaEvent projection does not appear in caption latency-critical call stacks.
6. Confirm plugin views are parsed from `unknown` and rendered without raw HTML.
7. Confirm current meeting-assistant and caption runtime tests pass unchanged.
8. Record the exact backend, frontend, Docker, and migration verification outputs in `docs/stage-records.md`.

Plan execution should stop at any checkpoint where the Docker sandbox cannot prove filesystem and network isolation. Do not downgrade silently to a cooperative child process.
