# Course Content Organizer Plugin Implementation Plan

> **For Codex:** REQUIRED SKILL: Use `executing-plans` to implement this plan task-by-task with review checkpoints.

**Goal:** Build and install the first scenario plugin, `com.matinier.course-organizer`, which produces timestamped realtime course notes while a browser-tab course plays and produces a versioned, evidence-closed final knowledge document from a Frozen Package on manual request or LiveKit completion.

**Architecture:** Keep all course-specific filtering, classification, language, and Map/Reduce logic inside a Docker-isolated plugin. Extend the host only with generic bounded model, delivery-snapshot, and versioned-document capabilities, then expose trusted document history and Markdown/JSON downloads through FastAPI and the existing plugin surface. Reuse `PackageBuilder` as the only Stage 1 Final → Frozen Package boundary; never call the plugin from the caption or translation hot path.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, asyncio JSON-RPC, existing DeepSeek structured provider, SQLite WAL, Ed25519-signed OCI image packages, Docker Desktop, pytest, Next.js 16, React 19, TypeScript 6, Vitest.

**Approved design:** `docs/plans/2026-08-28-course-content-organizer-plugin-design.md`

---

## Execution constraints

- Execute in the current directory. This workspace is not a valid Git repository, so worktree and commit steps are intentionally omitted. At every former commit boundary, record the exact changed files and run the stated checkpoint.
- Current Alembic head is `20260828_0025`; this feature owns additive migration `20260828_0026`.
- Never invoke a plugin, model, Package builder, or document renderer from `WorkerCaptionRuntime`, `TranslationRuntime`, Final persistence methods, or LiveKit audio callbacks.
- `SegmentRecord` and `TranslationSegmentRecord` remain authoritative. `PackageBuilder` remains the only component allowed to turn Stage 1 Final rows into a Stage 2 Package.
- The course plugin receives no database URL, provider key, project path, user-home path, Docker socket, or direct network.
- Model calls happen only through `model.invoke`. Delivery reads happen only through session-scoped Host capability adapters.
- Event handlers must persist the pending window through `state.put` before acknowledging its highest sequence. Model work runs as a background task and must not delay Event ack.
- A user command must return accepted before long model work begins. One MediaSession/language may have only one finalization task at a time.
- All new APIs return stable sanitized errors. No traceback, prompt payload, source path, provider credential, or raw Docker stderr reaches the browser or plugin.
- Every published fact keeps Frozen Package evidence. Do not silently accept a model item that cites an ID outside its input chunk.
- Preserve the diagnostic plugin, existing captions, exports, Stage 2 processing, and private meeting assistant behavior.

## Task 1: Freeze generic capability and document contracts

**Files:**
- Modify: `backend/app/plugins/capabilities.py`
- Create: `backend/app/plugins/document_contracts.py`
- Modify: `backend/app/settings.py`
- Modify: `backend/app/plugins/sdk_schemas.py`
- Modify: `backend/tests/test_plugin_broker.py`
- Create: `backend/tests/test_plugin_extended_contracts.py`
- Modify: `backend/tests/test_plugin_sdk_contract.py`
- Modify: `plugin-sdk/schemas/plugin-capabilities.schema.json` (generated)
- Modify: `plugin-sdk/README.md`

**Step 1: Write failing capability-contract tests**

Cover these exact public shapes and bounds:

```python
ModelInvokeInput(
    input_category="session_transcript",
    system_prompt="Return one JSON object.",
    user_prompt="Classify this window.",
    input_payload={"items": [{"item_id": "seg-1", "text": "A definition"}]},
    response_format="json_object",
    max_output_tokens=1024,
    timeout_seconds=20,
)

DeliveryPrepareInput(
    trigger="manual",
    output_language="zh-CN",
    final_sequence=42,
)

DeliveryQueryInput(
    package_id="package-1",
    document_kinds=("source_raw", "live_translation", "evidence_index"),
    language=None,
    after_item=0,
    limit=100,
)
```

Reject unknown fields, invalid BCP-47-like language values, negative sequences/cursors, more than 100 document kinds, payloads above the configured JSON bound, prompts over 32,000 characters, and model timeouts over 30 seconds.

Define generic document publication contracts:

```python
PluginDocumentEvidenceRef(
    item_id="item-1",
    source_segment_ids=("segment-1",),
    start_ms=1_000,
    end_ms=2_000,
)

PluginDocumentPublishInput(
    identity_key="course-notes:zh-CN",
    schema_name="matinier.course-notes",
    schema_version="1.0",
    language="zh-CN",
    trigger="manual",
    completeness="interim",
    source_package_id="package-1",
    content={"title": "课程内容整理", "sections": []},
    markdown="# 课程内容整理\n",
    evidence_refs=(...),
)
```

Require `identity_key` and schema-name syntax, `trigger` in `manual|session_completed|session_failed|session_cancelled`, `completeness` in `interim|complete|partial_terminal`, safe Markdown without raw HTML or `javascript/data/file` links, ordered evidence ranges, and bounded content/Markdown bytes.

**Step 2: Run tests and verify failure**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest `
  backend\tests\test_plugin_extended_contracts.py `
  backend\tests\test_plugin_sdk_contract.py -q
```

Expected: FAIL because delivery/document contracts and the published capability schema do not exist.

**Step 3: Implement the minimal frozen Pydantic contracts**

Replace the currently unused prompt-only model contract with structured fields. Target outputs:

```python
class ModelInvokeOutput(CapabilityOutput):
    output: dict[str, object]
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    finish_reason: str | None = Field(default=None, max_length=64)
    output_tokens: int = Field(default=0, ge=0, le=4096)

class DeliveryPrepareOutput(CapabilityOutput):
    package_id: str
    package_version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_language: str
    target_languages: tuple[str, ...]
    final_sequence: int = Field(ge=0)

class DeliveryQueryOutput(CapabilityOutput):
    package_id: str
    package_version: int = Field(ge=1)
    content_hash: str
    items: tuple[dict[str, object], ...]
    next_after_item: int | None
```

Add settings with conservative defaults and validation:

- `PLUGIN_MODEL_MAX_CONCURRENCY=2`
- `PLUGIN_MODEL_MAX_INPUT_CHARS=32000`
- `PLUGIN_MODEL_MAX_OUTPUT_TOKENS=4096`
- `PLUGIN_DOCUMENT_MAX_BYTES=196608`
- `PLUGIN_DELIVERY_MAX_PAGE_ITEMS=100`

Generate one `plugin-capabilities.schema.json` from a discriminated/union envelope containing all stable capability input/output models. Do not hand-edit generated schema content.

**Step 4: Regenerate schemas and run the checkpoint**

Run:

```powershell
Push-Location backend
try {
  .\.venv\Scripts\python.exe -m app.plugins.sdk_schemas ..\plugin-sdk\schemas
} finally { Pop-Location }
backend\.venv\Scripts\python.exe -m pytest `
  backend\tests\test_plugin_extended_contracts.py `
  backend\tests\test_plugin_sdk_contract.py `
  backend\tests\test_settings.py -q
```

Expected: all selected tests PASS; generated/published schemas are byte-for-structure identical.

**Checkpoint:** Record contract files and schema digest. Do not start persistence until the public contract is stable.

## Task 2: Add durable plugin documents and migration 0026

**Files:**
- Modify: `backend/app/persistence/models.py`
- Create: `backend/alembic/versions/20260828_0026_create_plugin_documents.py`
- Create: `backend/app/plugins/document_repository.py`
- Create: `backend/tests/test_plugin_document_repository.py`
- Create: `backend/tests/test_plugin_document_migration.py`

**Step 1: Write failing repository tests**

Test:

- first publication gets `document_version=1`;
- a second publication for the same `plugin_id + media_session_id + identity_key` gets version 2;
- another language/identity starts at version 1;
- old versions remain immutable and list newest first;
- exact idempotent retry returns the existing document;
- deleting an installed package sets `plugin_package_id` to null but preserves user documents;
- deleting a MediaSession cascades its documents;
- a source Frozen Package cannot be deleted while a document references it;
- content hash changes when content, Markdown, evidence, language, trigger, or Package hash changes.

**Step 2: Run tests and verify failure**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest `
  backend\tests\test_plugin_document_repository.py `
  backend\tests\test_plugin_document_migration.py -q
```

Expected: FAIL because `PluginDocumentRecord`, repository, and table do not exist.

**Step 3: Add the additive table and repository**

Create `plugin_documents` with:

- `id` UUID primary key;
- `plugin_id`, `plugin_version`;
- nullable `plugin_package_id → plugin_packages.id ON DELETE SET NULL`;
- `media_session_id → media_sessions.id ON DELETE CASCADE`;
- `source_package_id → result_packages.id ON DELETE RESTRICT`;
- `identity_key`, `document_version`, `schema_name`, `schema_version`, `language`;
- `trigger`, `completeness`, `status="published"`;
- `source_package_version`, `source_package_hash`;
- `content_json`, `evidence_refs_json`, `markdown_text`, `content_hash`, `created_at`;
- unique `(plugin_id, media_session_id, identity_key, document_version)`;
- indexes for `(media_session_id, plugin_id, created_at)` and `source_package_id`.

`PluginDocumentRepository.publish()` computes the canonical SHA-256 itself, allocates the next version in the same transaction, and never updates an existing row. It must validate that the supplied plugin package, MediaSession, and source Package exist before insert.

**Step 4: Run focused tests**

Run the Task 2 command again.

Expected: repository tests PASS and Alembic upgrade/downgrade test proves 0025 → 0026 → 0025 without losing pre-existing plugin/platform rows.

**Checkpoint:** Confirm only additive schema changes and no modification of `segments`, `translation_segments`, or existing Package documents.

## Task 3: Implement the session-bound delivery adapter

**Files:**
- Create: `backend/app/plugins/delivery.py`
- Modify: `backend/app/packages/repository.py`
- Create: `backend/tests/test_plugin_delivery_adapter.py`

**Step 1: Write failing delivery tests**

Seed an active browser-tab Session with Final source/translation rows and a MediaSession bridge. Test:

- `prepare()` calls the existing `PackageBuilder.build_baseline()` and returns a frozen Package;
- an active Session can create an interim Package;
- terminal Session metadata is reflected in a complete/partial terminal Package;
- no Final source returns the sanitized `Session has no Final captions` conflict;
- `query()` rejects a Package belonging to another MediaSession;
- source, translation, timeline, evidence, and metadata pages are deterministic;
- `after_item/limit` pagination has no gaps or duplicates;
- requested language selects matching translation but never hides evidence/source metadata;
- each returned transcript/evidence item stays below RPC limits.

**Step 2: Run and verify failure**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest `
  backend\tests\test_plugin_delivery_adapter.py -q
```

Expected: FAIL because `DeliveryCapabilityAdapter` does not exist.

**Step 3: Implement the adapter on the broker transaction**

Target interface:

```python
class DeliveryCapabilityAdapter:
    def __init__(self, db_session: Session, *, max_page_items: int) -> None: ...

    async def prepare(
        self,
        context: CapabilityExecutionContext,
        value: DeliveryPrepareInput,
    ) -> DeliveryPrepareOutput: ...

    async def query(
        self,
        context: CapabilityExecutionContext,
        value: DeliveryQueryInput,
    ) -> DeliveryQueryOutput: ...
```

Resolve `context.media_session_id → MediaSessionRecord.legacy_session_id`; reject missing legacy bridges. Use the same SQLAlchemy Session already owned by the Broker so capability invocation, Package creation, and audit commit atomically and cannot deadlock SQLite with a nested writer connection.

Add a read helper in `PackageRepository` that flattens selected Package documents into a canonical ordered page without changing the frozen Package. Never read `SegmentRecord` or `TranslationSegmentRecord` from `query()`.

**Step 4: Run tests**

Expected: all delivery tests PASS; existing `test_packages.py` and `test_packages_api.py` remain green.

**Checkpoint:** Verify Package queries operate only on frozen Package records after `prepare()`.

## Task 4: Activate bounded host model invocation

**Files:**
- Create: `backend/app/plugins/model_adapter.py`
- Modify: `backend/app/text_processing/provider.py`
- Modify: `backend/app/text_processing/deepseek_provider.py`
- Modify: `backend/app/plugins/broker.py`
- Modify: `backend/app/plugins/bootstrap.py`
- Create: `backend/tests/test_plugin_model_adapter.py`
- Modify: `backend/tests/test_plugin_broker.py`

**Step 1: Write failing model-adapter tests**

Use a Fake `StructuredTextProvider`. Verify:

- structured system/user/payload fields reach the Provider unchanged;
- output must parse as exactly one JSON object;
- arrays, scalars, malformed JSON, `finish_reason=length`, and oversized output fail safely;
- Provider configuration/auth/request errors map to stable capability failure without raw response bodies;
- plugin-supplied max tokens cannot exceed Host settings;
- two brokers share one semaphore, so configured concurrency 1 never overlaps model calls;
- prompts and transcript payloads are not written to log/audit payloads;
- no Provider key appears in plugin environment or output.

**Step 2: Run and verify failure**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest `
  backend\tests\test_plugin_model_adapter.py `
  backend\tests\test_plugin_broker.py -q
```

Expected: FAIL because the production registry does not register `model.invoke` and model concurrency is currently per-Broker rather than shared.

**Step 3: Implement the adapter and shared concurrency**

Target interface:

```python
class PluginModelCapabilityAdapter:
    def __init__(self, provider: StructuredTextProvider, *, max_input_chars: int,
                 max_output_tokens: int) -> None: ...

    async def __call__(self, context, value: ModelInvokeInput) -> ModelInvokeOutput: ...
```

Add optional token-usage metadata to `StructuredCompletionResult` without changing existing callers. `DeepSeekCompletionProvider` may read usage when present but must tolerate missing usage. JSON-decode the provider content and require a mapping before returning it.

Create one `asyncio.Semaphore(settings.plugin_model_max_concurrency)` on `PluginHostRuntime`; inject that same instance into every Broker. Do not construct a new semaphore per capability call.

Allow `PluginHostRuntime(..., structured_provider=fake)` injection for tests. Default composition reuses `DeepSeekCompletionProvider` settings. Registering the model capability must not perform a network call at startup.

**Step 4: Run tests**

Expected: model adapter and broker tests PASS; existing Stage 2 structured workflow tests remain green.

**Checkpoint:** Search plugin process environment construction and confirm no DeepSeek key or base URL is inherited.

## Task 5: Publish, validate, query, and export plugin documents

**Files:**
- Create: `backend/app/plugins/documents.py`
- Create: `backend/app/plugins/document_exporter.py`
- Create: `backend/app/api/plugin_documents.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/test_plugin_document_capability.py`
- Create: `backend/tests/test_plugin_documents_api.py`

**Step 1: Write failing document capability tests**

Test that `document.publish`:

- accepts a Package from the current MediaSession and rejects a foreign Package;
- validates each evidence ref exactly against the Package evidence index;
- rejects missing, forged, duplicated, reversed, or out-of-range evidence;
- rejects unsafe Markdown/raw HTML and documents above Host byte limits;
- publishes version 1 then version 2 without modifying version 1;
- returns document ID, version, identity, hash, language, trigger, and completeness;
- uses one Broker transaction and exact idempotent retry returns the same version.

**Step 2: Write failing API/export tests**

Define:

```text
GET /api/media-sessions/{media_session_id}/plugin-documents
GET /api/plugin-documents/{document_id}
GET /api/plugin-documents/{document_id}/export?format=markdown|json
```

Test optional `plugin_id`, `identity_key`, and `language` filters, newest-first versions, 404 for unknown/cross-session IDs, attachment names, JSON metadata/content, Markdown bytes, matching content-hash headers, and 400 for unsupported formats.

**Step 3: Run and verify failure**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest `
  backend\tests\test_plugin_document_capability.py `
  backend\tests\test_plugin_documents_api.py -q
```

Expected: FAIL because adapters/routes do not exist.

**Step 4: Implement publication and trusted exports**

`PluginDocumentCapabilityAdapter` must receive the current transaction's Session, verify Package/MediaSession ownership, load `evidence_index`, compare canonical refs, then call `PluginDocumentRepository.publish()`.

The exporter must never call a model. Markdown returns the validated stored Markdown. JSON is deterministic serialization of stored metadata, content, and evidence refs from the same document row. Use sanitized filenames such as:

```text
course-notes-zh-CN-v2.md
course-notes-zh-CN-v2.json
```

**Step 5: Run tests**

Expected: all Task 5 tests PASS and existing artifact/package export tests remain green.

**Checkpoint:** Confirm arbitrary plugin paths/URLs are never accepted by an export route.

## Task 6: Wire capabilities, command correlation, Broker transactions, and SDK docs

**Files:**
- Modify: `backend/app/plugins/broker.py`
- Modify: `backend/app/plugins/bootstrap.py`
- Modify: `backend/app/plugins/supervisor.py`
- Modify: `backend/app/api/media.py`
- Modify: `backend/tests/test_plugin_broker.py`
- Modify: `backend/tests/test_plugin_supervisor.py`
- Modify: `backend/tests/test_media_api.py`
- Modify: `docs/plugin-authoring.md`
- Modify: `docs/api-events.md`
- Modify: `docs/plugin-security.md`

**Step 1: Write failing composition tests**

Require the production registry to expose exactly the existing capabilities plus:

```python
add("model.invoke", "read", ModelInvokeInput, ModelInvokeOutput, model_adapter)
add("delivery.prepare", "local_write", DeliveryPrepareInput,
    DeliveryPrepareOutput, delivery_adapter, supports_idempotency=True)
add("delivery.query", "read", DeliveryQueryInput, DeliveryQueryOutput, delivery_adapter)
add("document.publish", "local_write", PluginDocumentPublishInput,
    PluginDocumentPublishOutput, document_adapter, supports_idempotency=True)
```

Test install-time permission rejection, current scope enforcement, idempotency-key requirement for `delivery.prepare` and `document.publish`, same-session Package validation, audit metadata without source payload, and revocation taking effect on the next call.

Test that `execute_plugin_command()` generates `command_id` before RPC, sends it in `command.execute`, and returns that same ID. A repeated browser request has a distinct command ID; the plugin decides task deduplication from its current running state.

**Step 2: Run and verify failure**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest `
  backend\tests\test_plugin_broker.py `
  backend\tests\test_plugin_supervisor.py `
  backend\tests\test_media_api.py -q
```

Expected: FAIL for missing adapters/capabilities and command correlation.

**Step 3: Implement same-transaction adapter injection**

When `_invoke_capability` opens `db_session`, construct delivery/document adapters with that exact Session and pass them to `CapabilityBroker`. Do not let adapters open nested database sessions. Register `model.invoke` with the shared runtime adapter/semaphore.

Extend `CapabilityBroker` dispatch by input model, not by untrusted capability strings. Keep the default-deny registry and revalidate accepted permission on every call.

Add `command_id` to Supervisor RPC params. Keep command execution bounded by the existing RPC timeout only for the short accepted response; long generation continues in a plugin background task.

**Step 4: Update public docs and run the checkpoint**

Document all inputs, outputs, permissions, idempotency, Package session binding, model credential boundary, size limits, and stable public errors. Run Task 6 tests plus SDK schema parity.

Expected: PASS.

**Checkpoint:** Re-run `rg -n "api_key|DATABASE_URL|docker.sock" backend/app/plugins plugin-sdk` and review every match for host-only handling.

## Task 7: Add a reusable asynchronous Python plugin runtime

**Files:**
- Create: `plugin-sdk/python/matinier_plugin/__init__.py`
- Create: `plugin-sdk/python/matinier_plugin/runtime.py`
- Create: `plugin-sdk/python/matinier_plugin/contracts.py`
- Create: `backend/tests/test_plugin_python_sdk.py`
- Modify: `plugin-sdk/README.md`

**Step 1: Write failing runtime tests**

Use in-memory async reader/writer fakes. Verify:

- Host requests are dispatched concurrently so heartbeat remains responsive during a long background job;
- plugin `capability()` sends a unique JSON-RPC ID and resolves the matching Host response;
- a request handler may await `state.put` before replying to `event.batch` without deadlock;
- background tasks can call capabilities after `command.execute` already returned accepted;
- errors are mapped to the stable public plugin error envelope;
- stdout writer serializes complete one-line JSON messages under concurrency;
- shutdown cancels owned tasks, resolves/aborts pending calls, and emits no banners/logs to stdout;
- stderr is the only log stream.

**Step 2: Run and verify failure**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest `
  backend\tests\test_plugin_python_sdk.py -q
```

Expected: FAIL because the Python runtime package does not exist.

**Step 3: Implement the minimal asyncio runtime**

Provide this plugin-facing interface:

```python
class PluginRuntime:
    def register(self, method: str, handler: AsyncHandler) -> None: ...
    async def capability(self, *, name: str, session_scope: str,
                         input_value: dict[str, object],
                         idempotency_key: str | None = None) -> dict[str, object]: ...
    def create_task(self, awaitable, *, name: str) -> asyncio.Task: ...
    async def run(self) -> None: ...
```

The reader loop distinguishes Host requests/notifications from responses to pending plugin calls. Spawn Host request handlers as owned tasks; keep a single async write lock. Use only the Python standard library so the course image needs no pip install or network.

**Step 4: Run tests**

Expected: all runtime tests PASS and the existing dependency-free diagnostic plugin tests still pass unchanged.

**Checkpoint:** Ensure the SDK runtime has no Host imports and can be copied into an isolated image.

## Task 8: Implement course-note domain models and deterministic filtering

**Files:**
- Create: `plugin-sdk/examples/course-organizer/course_organizer/__init__.py`
- Create: `plugin-sdk/examples/course-organizer/course_organizer/models.py`
- Create: `plugin-sdk/examples/course-organizer/course_organizer/filtering.py`
- Create: `plugin-sdk/examples/course-organizer/course_organizer/evidence.py`
- Create: `plugin-sdk/examples/course-organizer/course_organizer/prompts.py`
- Create: `backend/tests/test_course_organizer_domain.py`

**Step 1: Write failing multilingual rule tests**

Cover Chinese, English, and mixed input for:

- higher revision replaces lower revision;
- repeated/stutter text merges within a semantic/time window;
- pure filler is dropped;
- greeting, platform operation, advertisement, and course administration become `transition` or `background`;
- definitions, causal statements, comparisons, procedures, formulas, conclusions, and teacher emphasis become `knowledge_candidate`;
- demonstrations/stories/problems become `example`;
- incomplete, contradictory, or low-confidence fragments become `needs_confirmation`;
- every retained note has ordered milliseconds and input Segment IDs;
- no deterministic rule upgrades `needs_confirmation` into a confirmed fact.

**Step 2: Write failing model-output/evidence tests**

Define immutable domain shapes:

```python
RealtimeNote(id, note_type, title, body, start_ms, end_ms,
             source_segment_ids, evidence_item_ids, confidence_status,
             language, related_note_ids)

KnowledgeItem(id, category, topic_path, title, statement, explanation,
              evidence_item_ids, source_segment_ids, time_ranges,
              related_item_ids, confirmation_status)

CourseDocument(title, language, realtime_notes, categories, warnings)
```

Reject duplicate IDs, unknown categories, missing evidence, references outside the current model chunk, reversed ranges, unsupported confirmation status, and model-added facts without Package evidence.

**Step 3: Run and verify failure**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest `
  backend\tests\test_course_organizer_domain.py -q
```

Expected: FAIL because the course domain does not exist.

**Step 4: Implement minimal pure functions**

Keep filtering deterministic and side-effect free. Prompts must state the fixed final categories exactly:

- 核心概念
- 原理/机制
- 方法/步骤
- 案例
- 公式/数据
- 易错点与待确认问题

Model parsing must take an explicit allowed-evidence set and fail closed.

**Step 5: Run tests**

Expected: all domain tests PASS without Docker, database, LiveKit, or model credentials.

**Checkpoint:** Inspect fixtures and confirm no expected result relies only on keyword matching.

## Task 9: Build realtime batching, durable state, fallback, and plugin UI

**Files:**
- Create: `plugin-sdk/examples/course-organizer/course_organizer/session.py`
- Create: `plugin-sdk/examples/course-organizer/course_organizer/realtime.py`
- Create: `plugin-sdk/examples/course-organizer/course_organizer/view.py`
- Create: `plugin-sdk/examples/course-organizer/plugin.py`
- Create: `backend/tests/test_course_organizer_realtime.py`
- Modify: `backend/tests/test_plugin_sdk_contract.py`

**Step 1: Write failing realtime session tests**

With a Fake capability client and injected clock, prove:

- `session.open` restores `course-session` state and publishes an empty/ready view;
- `event.batch` handles source and translation Final revisions idempotently;
- the pending window is persisted with `state.put` before the Event response acknowledges its highest sequence;
- a window triggers at 60–90 seconds of media span or the configured character threshold;
- model work runs in the background and heartbeat stays responsive;
- successful structured output appends notes and republishes the view;
- model failure emits deterministic rule notes, sets degraded status, and schedules bounded retry;
- replay after crash does not duplicate notes;
- raw pending text is removed after a note batch commits;
- state stays under the Host quota for a multi-hour synthetic course.

Use constants in one config object, initially:

```python
RealtimeConfig(
    min_window_ms=60_000,
    max_window_ms=90_000,
    trigger_chars=1_600,
    retry_delays_seconds=(2, 10, 30),
    max_notes=500,
)
```

**Step 2: Write failing UI validation tests**

The panel must contain language controls and three tabs: realtime notes, final document, history. Realtime cards/timeline show classification, status, and `media_time_ms`. Declare only known commands. Verify the view passes Host `PluginUIViewDocument` parsing and stays under node/string bounds; large documents show a bounded preview and trusted-download instruction instead of truncating arbitrary JSON.

**Step 3: Run and verify failure**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest `
  backend\tests\test_course_organizer_realtime.py `
  backend\tests\test_plugin_sdk_contract.py -q
```

Expected: FAIL because the plugin session/realtime/view code is missing.

**Step 4: Implement realtime state machine**

The Event handler performs only revision merge, pending-state CAS, ack, and background-task scheduling. It must not wait for the model. Background completion uses CAS again before updating the view. Preserve evidence and timestamps through rule fallback.

**Step 5: Run tests**

Expected: realtime and SDK contract tests PASS.

**Checkpoint:** Add a test assertion showing `event.batch` returns before a blocked Fake model is released.

## Task 10: Implement manual/terminal finalization, Map/Reduce, versions, and language

**Files:**
- Create: `plugin-sdk/examples/course-organizer/course_organizer/finalizer.py`
- Create: `plugin-sdk/examples/course-organizer/course_organizer/language.py`
- Create: `plugin-sdk/examples/course-organizer/course_organizer/document.py`
- Modify: `plugin-sdk/examples/course-organizer/course_organizer/session.py`
- Modify: `plugin-sdk/examples/course-organizer/course_organizer/view.py`
- Create: `backend/tests/test_course_organizer_finalizer.py`

**Step 1: Write failing manual-generation tests**

Verify `generate_final`:

- returns `{"accepted": true, "job_id": command_id}` immediately;
- rejects/returns the current job if the same MediaSession/language is already running;
- calls `delivery.prepare(trigger=manual)` with an idempotency key derived from `command_id`;
- paginates `delivery.query` until `next_after_item=None`;
- runs per-page Map extraction then a bounded Reduce;
- validates every knowledge item against Package evidence;
- publishes `completeness=interim` through `document.publish`;
- keeps realtime Event processing active during finalization;
- leaves the old document/version visible on failure.

**Step 2: Write failing terminal-generation tests**

Verify `session.completed` flushes the last realtime window and schedules exactly one deterministic terminal job. The idempotency key must survive container restart and yield one complete version. `session.failed/cancelled` with Final input may publish `partial_terminal`; without Final input it publishes no empty document and shows the stable reason.

**Step 3: Write failing language/version tests**

Cover:

- default translation language, source fallback, and custom validated language;
- language change localizes all existing notes in chunks and swaps only after success;
- language failure keeps the old view intact;
- language identities have independent version histories;
- manual interim remains after terminal complete;
- Markdown and JSON are rendered once from the same `CourseDocument` structure;
- Markdown has exactly the two approved top-level sections and `HH:MM:SS.mmm` coordinates.

**Step 4: Run and verify failure**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest `
  backend\tests\test_course_organizer_finalizer.py -q
```

Expected: FAIL because finalizer/language/document modules do not exist.

**Step 5: Implement bounded Map/Reduce and retries**

Map only over the current delivery page. Each Map output carries evidence IDs. Reduce may merge/deduplicate and build topic paths but cannot invent evidence IDs. Validate again before publication. Allow one model-repair call for invalid Schema; thereafter enter `waiting_retry` with `(2, 10, 30)` bounded delays and expose manual retry.

The document builder emits:

```python
{
  "title": "课程内容整理",
  "language": target_language,
  "parts": [
    {"id": "realtime-notes", "title": "第一部分：实时知识笔记", ...},
    {"id": "knowledge", "title": "第二部分：课程知识点整理", ...},
  ],
}
```

**Step 6: Run tests**

Expected: all finalizer tests PASS, including restart/idempotency and evidence closure.

**Checkpoint:** Manually inspect one generated Markdown/JSON fixture for exact two-part structure and no unsupported claim.

## Task 11: Add course plugin manifest, Docker image, generic signed packager

**Files:**
- Create: `plugin-sdk/examples/course-organizer/plugin.json`
- Create: `plugin-sdk/examples/course-organizer/Dockerfile`
- Create: `plugin-sdk/examples/course-organizer/README.md`
- Create: `backend/scripts/package_plugin.py`
- Modify: `backend/scripts/package_diagnostic_plugin.py`
- Create: `backend/scripts/package_course_organizer_plugin.py`
- Modify: `backend/tests/test_plugin_sdk_contract.py`
- Create: `backend/tests/test_course_plugin_packaging.py`

**Step 1: Write failing manifest/packager tests**

Manifest requirements:

```json
{
  "id": "com.matinier.course-organizer",
  "name": "课程内容整理",
  "version": "1.0.0",
  "publisher": "Matinier Development",
  "host_api": ">=1.0 <2.0",
  "subscriptions": [
    "session.cancelled", "session.completed", "session.failed",
    "transcript.final", "translation.final"
  ],
  "permissions": [
    "delivery.prepare", "delivery.query", "document.publish",
    "model.invoke", "state.get", "state.put", "ui.publish"
  ],
  "commands": ["generate_final", "retry_final", "select_version", "set_language"]
}
```

Assert no `network.fetch`. Validate resources within Host limits and all UI actions map to declared commands.

The generic packager must refuse overwrite, refuse output inside source/build context, require an external Ed25519 key, build with an explicit Dockerfile/context argument list, save OCI image tar, calculate digests, and create deterministic ZIP member metadata. Diagnostic wrapper must continue to produce the existing package shape.

**Step 2: Run and verify failure**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest `
  backend\tests\test_course_plugin_packaging.py `
  backend\tests\test_plugin_sdk_contract.py -q
```

Expected: FAIL because manifest, Dockerfile, and generic packager are absent.

**Step 3: Implement build context safely**

Build the course image from repository root with an explicit Dockerfile so it can copy only:

- `plugin-sdk/python/matinier_plugin`;
- `plugin-sdk/examples/course-organizer/plugin.py`;
- `plugin-sdk/examples/course-organizer/course_organizer`.

The Dockerfile creates an unprivileged user and has no package-manager/network step. Do not copy the repository root wholesale into the final image.

**Step 4: Run non-Docker tests**

Expected: all packager/SDK tests PASS and `--help` for both wrappers is side-effect free.

**Checkpoint:** Do not create or store a private key in the repository.

## Task 12: Add trusted document history/downloads to the frontend

**Files:**
- Modify: `frontend/types/plugins.ts`
- Modify: `frontend/lib/api.ts`
- Create: `frontend/components/plugins/plugin-document-shelf.tsx`
- Modify: `frontend/components/plugins/plugin-surface.tsx`
- Modify: `frontend/components/plugins/plugin-manager.tsx`
- Modify: `frontend/app/globals.css`
- Create: `frontend/lib/plugin-document-state.ts`
- Create: `frontend/lib/plugin-document-state.test.ts`
- Modify: `frontend/lib/plugin-ui-schema.test.ts`

**Step 1: Write failing state/API tests**

Define `PluginDocumentSummary/Detail` types matching the new API. Test grouping by plugin/identity/language, newest selection, preservation of interim + complete versions, safe export URL construction, loading/error/empty state, and no trust in plugin-provided paths or URLs.

**Step 2: Run and verify failure**

Run:

```powershell
npm run test -- --reporter=dot
```

from `frontend`.

Expected: new tests FAIL because document types/state/API do not exist.

**Step 3: Implement the trusted document shelf**

`PluginSurface` already polls views. Add a bounded document refresh using `mediaSessionId`, render version/language/trigger/completeness metadata, and generate Markdown/JSON links only through `getPluginDocumentExportUrl(documentId, format)`. Keep course content rendering inside the validated plugin view; the trusted shelf supplies history/download controls.

Update the manager's capability effect mapping so `delivery.prepare` and `document.publish` are `local_write`; `model.invoke`/`delivery.query` remain `read`. Do not request ephemeral grants for these internal session-scoped capabilities.

**Step 4: Run frontend checks**

Run:

```powershell
npm run test -- --reporter=dot
npm run typecheck
npm run build
```

Expected: all Vitest files PASS, TypeScript exits 0, and Next build includes `/plugins` and `/sessions/[sessionId]`.

**Checkpoint:** Verify unsafe plugin view fallback still renders and download controls do not use raw view values.

## Task 13: Prove the complete workflow with Fake Host model/container

**Files:**
- Create: `backend/tests/test_course_organizer_e2e.py`
- Modify: `backend/tests/test_plugin_framework_e2e.py`
- Modify: `backend/tests/test_private_meeting_agent_core.py`
- Modify: `backend/tests/test_live_translation_provider.py` only if a compatibility assertion belongs there

**Step 1: Build a deterministic Fake model script**

Return fixed JSON by an explicit task marker in `input_payload`:

- realtime classification;
- Map extraction;
- Reduce hierarchy;
- note localization;
- one invalid-first-then-repaired response;
- retryable unavailable response.

No cloud/network call is allowed in this test.

**Step 2: Write the end-to-end scenario**

1. Seed an active browser-tab Session with English Final and Chinese translation.
2. Install/enable the course plugin through the real PackageStore using a Fake image importer/runtime.
3. Resolve MediaSession and deliver enough events to trigger realtime notes.
4. Assert categories and exact video anchors in the published view.
5. Execute `generate_final` in Chinese and assert command returns before blocked Fake model release.
6. Release model, assert interim document v1 and matching Markdown/JSON.
7. Add more Final captions while realtime notes continue.
8. Mark Session completed and project `session.completed`.
9. Assert one complete document v2, v1 preserved, Package hashes differ as expected, and every final knowledge item closes to v2 evidence.
10. Switch to a custom language and assert independent identity/version.
11. Crash/restart the Fake container; assert durable cursor and no duplicate terminal document.
12. Make model unavailable; assert rule-based realtime note and final waiting/retry state.
13. Build private meeting context from the same Session and assert caption evidence remains available.

**Step 3: Run focused E2E repeatedly**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest `
  backend\tests\test_course_organizer_e2e.py `
  backend\tests\test_plugin_framework_e2e.py `
  backend\tests\test_private_meeting_agent_core.py -q
```

Then run the new course E2E five sequential times.

Expected: every run PASS; no intermittent SQLite savepoint/lock, duplicate version, stale scope, or leaked task warning.

**Checkpoint:** Stop if evidence closure, manual/terminal versioning, or model-unavailable recovery is not deterministic.

## Task 14: Run the real signed Docker course-plugin smoke

**Files:**
- Create: `backend/smoke/course_organizer_plugin_smoke.py`
- Modify: `docs/plugin-operations.md`
- Modify: `docs/plugin-security.md`

**Step 1: Implement a credential-free real-container smoke**

Use:

- real Docker image build/save/import;
- generated temporary Ed25519 key outside the repository;
- real PackageStore, Supervisor, JSON-RPC, Broker, SQLite, PackageBuilder and document APIs;
- injected deterministic Fake Structured Provider on the Host;
- real course plugin container with `--network none`.

The smoke must print exactly one final JSON object containing at least:

```json
{
  "plugin_ready": true,
  "realtime_notes": true,
  "manual_interim": true,
  "terminal_complete": true,
  "evidence_closed": true,
  "exports_match": true,
  "language_selected": true,
  "cursor_resumed": true,
  "network_isolated": true,
  "secret_blocked": true,
  "caption_regression": false
}
```

Inspect the running container and require `NetworkMode=none`, empty `Mounts`, read-only root, `CapDrop=[ALL]`, and `no-new-privileges`. Attempt direct socket access and confirm failure. Set a Host-only sentinel secret and confirm it is absent inside the container. Kill the container during/after realtime work and verify generation increases and processing resumes from durable state/cursor.

**Step 2: Run existing isolation smoke first**

Run with explicit Docker authorization:

```powershell
backend\.venv\Scripts\python.exe backend\smoke\plugin_sandbox_smoke.py
```

Expected: all existing isolation booleans true.

**Step 3: Run the course smoke**

```powershell
backend\.venv\Scripts\python.exe backend\smoke\course_organizer_plugin_smoke.py
```

Expected: exact final booleans above. The script removes only its exact temporary container/image/tag and temporary directory; it must not remove unrelated Docker data.

**Step 4: Re-run the generic framework smoke**

```powershell
backend\.venv\Scripts\python.exe backend\smoke\plugin_framework_smoke.py
```

Expected: `plugin_ready=true`, `cursor_resumed=true`, `network_isolated=true`, `caption_regression=false`.

**Checkpoint:** Stop release if any Docker proof fails. Do not downgrade to an in-process or ordinary child-process plugin.

## Task 15: Documentation, migration verification, full regression, and stage record

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `docs/architecture.md`
- Modify: `docs/api-events.md`
- Modify: `docs/plugin-authoring.md`
- Modify: `docs/plugin-operations.md`
- Modify: `docs/plugin-security.md`
- Modify: `docs/stage-records.md`
- Modify: `plugin-sdk/README.md`

**Step 1: Update user/developer documentation**

Document:

- installing and enabling `com.matinier.course-organizer`;
- accepting seven exact permissions and why none grants direct network;
- realtime-only behavior during playback;
- manual interim vs automatic terminal complete versions;
- language selection and fallback;
- coordinate limitations for external captured tabs;
- trusted Markdown/JSON downloads;
- model-unavailable degraded/retry behavior;
- Package/evidence boundary;
- exact settings and current migration head `20260828_0026`;
- Docker isolation and single-instance SQLite limits.

Do not claim webpage DOM extraction, video seeking, OCR, DOCX/PDF, external research, multi-tenancy, horizontal scale, or sandbox guarantees beyond the tested Docker boundary.

**Step 2: Verify migration from a clean temporary SQLite database**

Create an explicit temporary database under `backend/data`, run:

```powershell
Push-Location backend
try {
  $env:DATABASE_URL='sqlite:///./data/course-plugin-migration-verify.db'
  .\.venv\Scripts\python.exe -m alembic upgrade head
  .\.venv\Scripts\python.exe -m alembic current
  .\.venv\Scripts\python.exe -m alembic heads
} finally {
  Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
  Pop-Location
}
```

Expected: `20260828_0026 (head)`. After verifying the resolved path is exactly the named file under `backend/data`, remove that database and its `-wal`/`-shm` sidecars.

**Step 3: Run full backend verification sequentially**

```powershell
backend\.venv\Scripts\python.exe -m compileall -q backend\app backend\tests
backend\.venv\Scripts\python.exe -m pytest backend\tests -q
```

Expected: compile exits 0; all tests pass with only explicitly documented external-condition skips and existing deprecation warnings.

**Step 4: Run full frontend verification**

From `frontend`:

```powershell
npm run test -- --reporter=dot
npm run typecheck
npm run build
```

Expected: all tests PASS, typecheck exits 0, production build succeeds.

**Step 5: Run all Docker smokes sequentially**

Run Task 14 commands in order, not concurrently with pytest. Record exact final JSON outputs and Docker Client/Engine versions.

**Step 6: Record the exact closure evidence**

Append to `docs/stage-records.md`:

- migration head;
- backend/frontend exact counts;
- course Fake E2E repeated-run result;
- all three Docker smoke JSON objects;
- Package/document IDs and hashes from the credential-free smoke;
- proof that interim and complete versions coexist;
- language/evidence/export/cursor results;
- known limits and next extension boundary;
- confirmation that no cloud model, model key, or external web call was required for automated acceptance.

**Final acceptance:** The feature is complete only when the signed real container produces realtime notes, a manual interim document, an automatic terminal complete document, evidence-closed Markdown/JSON, language selection, degraded recovery, and crash-resumed state while all pre-existing caption/meeting/plugin regressions remain green.
