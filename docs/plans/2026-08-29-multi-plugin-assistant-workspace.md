# Multi-Plugin Assistant Workspace Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Permanently connect the signed course organizer to the development instance and add a generic `/assistants` workspace that can host and switch among multiple plugins for current or historical caption sessions.

**Architecture:** Keep plugin installation, signing, permissions, runtime, view rendering, and documents generic. Add a server-owned allowlisted built-in catalog that packages local first-party plugins through the existing PackageStore, and refactor the current Room-only plugin surface into reusable session/plugin state plus a dedicated master-detail workspace. The Room remains the realtime caption surface and links into the workspace through URL-addressable Session/plugin selection.

**Tech Stack:** FastAPI, Pydantic, asyncio, Docker CLI, Ed25519, SQLAlchemy/Alembic, Next.js 16 App Router, React 19, TypeScript, Vitest, pytest.

---

## Constraints and checkpoints

- The workspace is not a plugin-specific frontend. It only renders the existing closed plugin UI schema.
- Built-in source paths are selected by a server-side allowlist. No request body may contain a source path, Dockerfile, command, image tag, output path, or signing-key path.
- Dynamic local signing is development-only and guarded by `PLUGIN_BUILTIN_BUILD_ENABLED`; production rejects it.
- Installation remains two-phase and admin-token protected. Built-ins do not bypass signature, fingerprint, exact permission acceptance, immutable version, or Docker isolation checks.
- Keep the dynamic signing key and build cache outside the repository build context. Only PackageStore's verified installed copy may enter `DATA_DIR/plugins/packages`.
- Do not expose the signing key or admin token in API payloads, browser logs, command output, stage records, or final responses.
- There is no Git repository in this workspace. Replace commit steps with explicit test/checkpoint records in `docs/stage-records.md`; do not invent commit hashes.

## Task 1: Define and validate the built-in plugin registry

**Files:**

- Create: `backend/app/plugins/builtins.py`
- Create: `backend/tests/test_plugin_builtins.py`
- Modify: `backend/app/settings.py`
- Modify: `.env.example`
- Modify: `backend/tests/test_settings.py`

**Step 1: Write failing registry and settings tests**

Cover:

```python
def test_registry_contains_exact_course_descriptor():
    descriptor = registry.require("com.matinier.course-organizer")
    assert descriptor.permissions == (
        "delivery.prepare",
        "delivery.query",
        "document.publish",
        "model.invoke",
        "state.get",
        "state.put",
        "ui.publish",
    )
    assert descriptor.source_dir.is_relative_to(PROJECT_ROOT)


def test_registry_rejects_unknown_or_path_shaped_ids():
    for value in ("unknown", "../course-organizer", "C:\\course"):
        with pytest.raises(LookupError):
            registry.require(value)


def test_production_rejects_dynamic_builtin_signing():
    with pytest.raises(ValueError):
        Settings(app_env="production", plugin_builtin_build_enabled=True, ...)
```

**Step 2: Run tests and verify they fail**

```powershell
backend\.venv\Scripts\python.exe -m pytest `
  backend\tests\test_plugin_builtins.py `
  backend\tests\test_settings.py -q
```

Expected: new imports/settings are missing.

**Step 3: Implement the minimal contracts**

Add frozen `BuiltinPluginDescriptor` and `BuiltinPluginRegistry`. The only initial descriptor is the course organizer and contains display name, description, version, fixed source identifier, exact permissions, and `dynamic_build_available` derived from Settings. Keep filesystem paths internal; response contracts added later must not serialize them.

Add:

```dotenv
PLUGIN_BUILTIN_BUILD_ENABLED=false
```

Add an explicit absolute `PLUGIN_BUILTIN_WORK_DIR` setting for cache and signing material. Its default resolves to the platform-local application-data directory outside `PROJECT_ROOT`; validation rejects paths inside the repository, relative paths, and dynamic build enabled in production. Installed verified packages still use the existing `DATA_DIR/plugins/packages` path.

**Step 4: Run focused tests**

Expected: all focused tests pass.

## Task 2: Build and cache signed built-in packages safely

**Files:**

- Create: `backend/app/plugins/builtin_packages.py`
- Modify: `backend/scripts/package_course_organizer_plugin.py`
- Create: `backend/tests/test_builtin_plugin_packages.py`
- Modify: `backend/tests/test_course_plugin_packaging.py`

**Step 1: Write failing package-service tests**

Inject a fake package builder and assert:

- the service generates one Ed25519 PEM key with exclusive creation and reuses it;
- package/key/cache paths resolve below the configured external built-in work root and outside `PROJECT_ROOT`;
- symlinks and escaped paths are rejected;
- the source digest covers the Dockerfile, manifest, plugin entrypoint, course package, and public Python SDK runtime copied by the Dockerfile;
- two concurrent `prepare("com.matinier.course-organizer")` calls invoke the builder once;
- an unchanged digest reuses an existing non-empty package;
- build failure removes only the incomplete target and preserves the last valid cached package;
- no return value or raised public error contains private key bytes/path.

**Step 2: Run tests and verify failure**

```powershell
backend\.venv\Scripts\python.exe -m pytest `
  backend\tests\test_builtin_plugin_packages.py `
  backend\tests\test_course_plugin_packaging.py -q
```

**Step 3: Implement `BuiltinPluginPackageService`**

Use per-plugin `asyncio.Lock`, `asyncio.to_thread`, atomic temporary output in the external target directory, and `Path.replace` after package validation. The builder callable is injected in tests and defaults to `package_course_plugin`. Generate the key with `Ed25519PrivateKey.generate()` and `serialization.NoEncryption()`; request mode `0o600` where the platform honors it. Preserve the existing packager rule that key and output must remain outside the repository build context.

The service returns only the cached `.plugin.zip` path to trusted Host code. It never imports the image or installs the package directly.

**Step 4: Run focused tests**

Expected: package/security tests pass without Docker because the builder is fake.

## Task 3: Expose the built-in catalog through the existing two-phase Host flow

**Files:**

- Modify: `backend/app/plugins/bootstrap.py`
- Modify: `backend/app/api/plugins.py`
- Modify: `backend/app/main.py` only if dependency construction requires it
- Modify: `backend/tests/test_plugins_api.py`
- Modify: `backend/tests/test_plugin_framework_e2e.py`

**Step 1: Extend API fakes and write failing endpoint tests**

Required behavior:

```text
GET  /api/plugins/builtins
POST /api/plugins/builtins/{plugin_id}/packages:inspect
```

Assertions:

- list is read-only and contains no server path/key/image command;
- inspect returns 401/403 without the existing admin token;
- dynamic build disabled returns 409 with stable detail;
- unknown ID returns 404;
- build/package failure returns generic 500 without internal paths;
- successful inspect returns the existing `PluginInspection` shape and exact seven permissions;
- confirm/install/enable still use existing endpoints and registries.

**Step 2: Run API tests and verify failure**

```powershell
backend\.venv\Scripts\python.exe -m pytest `
  backend\tests\test_plugins_api.py `
  backend\tests\test_plugin_framework_e2e.py -q
```

**Step 3: Add runtime methods and routes**

Add to `PluginHostRuntimeLike` and `PluginHostRuntime`:

```python
def list_builtin_plugins(self) -> list[dict[str, object]]: ...
async def inspect_builtin_plugin(self, plugin_id: str) -> dict[str, object]: ...
```

`inspect_builtin_plugin` calls the package service, then the same `PackageStore.inspect` used for uploaded ZIPs. The route accepts no body. Map `LookupError` to 404, disabled build to 409, and all hidden build failures to a stable 500.

**Step 4: Run focused tests**

Expected: API and framework tests pass; uploaded package behavior is unchanged.

## Task 4: Create the generic multi-plugin workspace state model

**Files:**

- Create: `frontend/lib/assistant-workspace-state.ts`
- Create: `frontend/lib/assistant-workspace-state.test.ts`
- Modify: `frontend/types/plugins.ts`
- Modify: `frontend/lib/api.ts`

**Step 1: Write failing pure-state tests**

Test helpers/reducer for:

- merging installed plugins, views, and documents by plugin ID;
- preserving installed plugins with no view and document-only historical plugins;
- runtime status priority (`quarantined/crashed/degraded` before waiting/ready);
- deterministic default plugin selection and requested-plugin fallback;
- per-view input values keyed by `mediaSessionId:pluginId:surface:viewId`;
- clearing only the old Session state on Session switch;
- filtering documents by selected plugin without trusting plugin-provided URLs.

**Step 2: Run Vitest and verify failure**

```powershell
Set-Location frontend
npm run test -- assistant-workspace-state.test.ts --reporter=dot
```

**Step 3: Implement types/API/state**

Add `BuiltinPluginSummary`, workspace catalog entry/status types, `listBuiltinPlugins`, and `inspectBuiltinPlugin`. Keep state functions pure so they run in the existing Node Vitest environment without adding jsdom or a new dependency.

**Step 4: Run focused frontend tests**

Expected: focused tests pass.

## Task 5: Build `/assistants` and refactor reusable plugin-session loading

**Files:**

- Create: `frontend/app/assistants/page.tsx`
- Create: `frontend/components/assistants/assistant-workspace.tsx`
- Create: `frontend/components/assistants/assistant-session-selector.tsx`
- Create: `frontend/components/assistants/assistant-catalog.tsx`
- Create: `frontend/components/assistants/assistant-detail.tsx`
- Create: `frontend/components/plugins/use-media-assistant-workspace.ts`
- Modify: `frontend/components/plugins/plugin-document-shelf.tsx`
- Modify: `frontend/components/plugins/plugin-surface.tsx`
- Modify: `frontend/app/globals.css`
- Modify: `frontend/lib/assistant-workspace-state.test.ts`

**Step 1: Add failing state/selection tests**

Cover URL selection normalization (`session`, `plugin`), current/running Session preference, latest-history fallback, disposed-generation behavior, and selected-plugin document filtering.

**Step 2: Implement the page and shared hook**

The page loads Sessions with `listSessions`, resolves one MediaSession, then loads installed plugins/views/documents in parallel. It writes selection with `history.replaceState`, preserving reload/deep-link behavior without introducing a global client store.

The hook owns polling and generation guards. It parses each view independently and retains the previous validated document for the same composite view key. `AssistantDetail` renders all views belonging to the selected plugin and passes only that plugin to `PluginDocumentShelf`.

Required empty states: no Sessions, plugin framework unavailable, plugin not installed, disabled, waiting for view, only historical documents, document error, invalid view, runtime degraded.

**Step 3: Run tests, typecheck, and build**

```powershell
Set-Location frontend
npm run test -- --reporter=dot
npm run typecheck
npm run build
```

Expected: all tests pass and build routes include `/assistants`.

## Task 6: Replace the Room expansion with a compact launcher and shared navigation

**Files:**

- Create: `frontend/components/assistants/room-assistant-launcher.tsx`
- Modify: `frontend/components/room-studio.tsx`
- Modify: `frontend/components/plugins/plugin-manager.tsx`
- Modify: `frontend/app/globals.css`
- Modify: `frontend/lib/assistant-workspace-state.test.ts`

**Step 1: Write failing launcher-summary tests**

Pure summary tests calculate connected count, error count, latest view/document update, and the exact `/assistants?session=<id>` URL.

**Step 2: Implement launcher/navigation**

Replace `<PluginSurface>` in the Room aside with `RoomAssistantLauncher`. Keep a visible top-level “助手工作区” link in Room controls, and cross-link `/assistants`, `/plugins`, and `/` in page headers. Do not remove the existing generic renderer; it is now consumed by `AssistantDetail`.

**Step 3: Run frontend verification**

Expected: launcher tests, full Vitest, typecheck, and production build pass at desktop and narrow CSS breakpoints.

## Task 7: Add the project built-in catalog to `/plugins`

**Files:**

- Modify: `frontend/components/plugins/plugin-manager.tsx`
- Modify: `frontend/lib/plugin-manager-state.ts`
- Modify: `frontend/lib/plugin-manager-state.test.ts`
- Modify: `frontend/types/plugins.ts`
- Modify: `frontend/app/globals.css`

**Step 1: Write failing reducer tests**

Add a source discriminator (`upload` or `builtin`) and test:

- catalog loading and installed-state reconciliation;
- built-in inspect without a File;
- exact permission toggles;
- “confirm, install and enable” sequence;
- install succeeds but enable fails -> installed/disabled warning;
- repeated click while inspecting/installing is disabled;
- uploaded package flow remains unchanged.

**Step 2: Implement the catalog UI**

Render “项目内置插件” cards before manual upload. A course card shows purpose, version, build availability, installed/runtime state, and a “检查内置插件” action. Reuse the existing inspection/fingerprint/permission confirmation panel. For a built-in source, the final explicit action calls install then enable; never auto-accept permissions.

**Step 3: Run frontend verification**

Expected: full frontend tests/typecheck/build pass.

## Task 8: Prove real built-in packaging and multi-plugin isolation

**Files:**

- Modify: `backend/smoke/course_organizer_plugin_smoke.py`
- Modify: `backend/tests/test_course_organizer_e2e.py`
- Modify: `backend/tests/test_plugin_framework_e2e.py`
- Modify: `docs/plugin-operations.md`
- Modify: `docs/plugin-security.md`

**Step 1: Change the course smoke to use the built-in inspect endpoint**

The smoke must no longer upload a prebuilt course ZIP. Enable built-in build in its temporary Settings, call the built-in inspect endpoint with the admin token, explicitly confirm the seven permissions, install, and enable. Continue verifying realtime notes, manual interim, terminal complete, language selection, evidence, exports, crash cursor, no network, no mounts, read-only root, no secret, and no caption regression.

**Step 2: Add a two-plugin deterministic E2E**

Install course organizer and diagnostic plugins for one MediaSession. Assert both view envelopes coexist, commands address the correct plugin/view composite key, and document listing/filtering never attributes course documents to diagnostic.

**Step 3: Run focused tests five times**

```powershell
backend\.venv\Scripts\python.exe -m pytest `
  backend\tests\test_course_organizer_e2e.py `
  backend\tests\test_plugin_framework_e2e.py -q
```

Repeat sequentially five times. Expected: all five runs pass.

**Step 4: Run all Docker smokes sequentially**

```powershell
backend\.venv\Scripts\python.exe backend\smoke\plugin_sandbox_smoke.py
backend\.venv\Scripts\python.exe backend\smoke\course_organizer_plugin_smoke.py
backend\.venv\Scripts\python.exe backend\smoke\plugin_framework_smoke.py
```

Expected: all isolation booleans true; course functional booleans true; generic cursor/network/caption booleans true. Confirm exact smoke images/containers are cleaned.

## Task 9: Migrate, provision, and verify the actual development instance

**Files:**

- Modify locally but do not disclose: `.env`
- Modify: `README.md`
- Modify: `docs/api-events.md`
- Modify: `docs/plugin-operations.md`
- Modify: `docs/stage-records.md`

**Step 1: Back up and migrate the named development database**

Confirm API/Worker are stopped. Use SQLite backup to a precisely named file under `backend/data/backups`, then:

```powershell
Push-Location backend
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
Pop-Location
```

Expected: `20260828_0026 (head)`. Run `PRAGMA quick_check` and confirm existing Session/Segment counts are unchanged. Do not delete the backup during this task.

**Step 2: Configure local administration securely**

If `.env` has no `PLUGIN_ADMIN_TOKEN`, generate a cryptographically random value and update only that key without printing it. Set `PLUGIN_BUILTIN_BUILD_ENABLED=true` and an absolute external `PLUGIN_BUILTIN_WORK_DIR` for this development instance. Resolve and verify that work directory is outside `PROJECT_ROOT` before creating it. Do not copy these settings to frontend environment files.

**Step 3: Start the existing development stack and provision through HTTP**

Start Docker/LiveKit/API/Worker/Frontend with the existing launcher. Use the local API and the token read in-process from Settings to perform:

```text
GET built-in catalog
POST course built-in inspect
POST installation with exact seven accepted permissions and confirmed fingerprint
POST enable
```

Expected: `com.matinier.course-organizer` is persisted as enabled/ready in the real development database and its package remains under `backend/data/plugins/packages` after API restart.

**Step 4: Run full regression**

```powershell
backend\.venv\Scripts\python.exe -m compileall -q backend\app backend\tests
backend\.venv\Scripts\python.exe -m pytest backend\tests -q
Set-Location frontend
npm run test -- --reporter=dot
npm run typecheck
npm run build
```

Expected: all tests pass with only documented external-condition skips and existing deprecation warnings.

**Step 5: Perform browser acceptance**

Using the running local app:

1. Verify Room exposes the visible assistant workspace entry.
2. Open `/assistants?session=<course-session>`.
3. Verify Session selection and course/diagnostic plugin switching.
4. Play captured-tab course audio and confirm realtime notes appear without blocking captions.
5. Generate interim, end the Session, confirm complete vNext, language selection, history, and trusted Markdown/JSON links.
6. Reload API/page and confirm installed plugin, selected Session/plugin URL, views, and document history recover.

**Step 6: Record exact closure evidence**

Append migration head, backup path, test counts, Docker versions/JSON, installed package identity/status, multi-plugin browser observations, and known single-instance/Docker boundary to `docs/stage-records.md`. Record only that the signing work root was verified outside the repository; do not record tokens or private-key paths.

---

## Final acceptance

The work is complete only when the real development database is at `20260828_0026`, the course plugin remains installed and enabled after restart, `/assistants` can switch at least two plugins for one Session without state/document cross-contamination, Room exposes the workspace clearly, and all existing caption/meeting/plugin regressions plus real Docker isolation smokes remain green.
