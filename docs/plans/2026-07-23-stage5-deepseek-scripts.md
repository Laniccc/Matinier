# Stage 5 DeepSeek Processed Scripts Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Generate versioned, traceable DeepSeek-processed timeline scripts from durable Final captions without changing or weakening the authoritative source transcript path.

**Architecture:** Keep Stage 3/4 realtime captions, SQLite Final rows, source exports, and `livecaption.events.v1` unchanged. A bounded post-processing service snapshots normalized Final segments, calls the official OpenAI-compatible DeepSeek Chat Completion API with JSON Output, validates every source reference, renders independent Markdown, and persists only fully valid results as append-only versions. FastAPI owns generation/list/detail/export endpoints; the frontend calls only FastAPI and keeps source captions visible when generation fails.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, SQLite, httpx, pytest, Next.js 16, React 19, TypeScript 6, pnpm.

**Current API contract verified on 2026-07-23:** Official DeepSeek documentation lists `deepseek-v4-flash` and `deepseek-v4-pro`, base URL `https://api.deepseek.com`, Chat Completion `/chat/completions`, and JSON Output via `response_format={"type":"json_object"}`. The prompt must explicitly request JSON; empty content and `finish_reason="length"` must be treated as invalid output.

**Scope boundary:** No change to raw/display Final captions, source exports, realtime events, ASR, translation, Draft persistence, manual editing, uploads, background queue, or Stage 6 expanded session state machine. This directory is not a Git repository, so commit steps are intentionally omitted.

---

### Task 1: Stage 5 settings and append-only ProcessedScript persistence

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Modify: `backend/app/settings.py`
- Modify: `.env.example`
- Modify: `backend/app/persistence/models.py`
- Create: `backend/app/persistence/scripts.py`
- Create: `backend/alembic/versions/20260723_0004_create_processed_scripts.py`
- Modify: `backend/tests/test_settings.py`
- Create: `backend/tests/test_script_repository.py`

**Step 1: Write failing settings and repository tests**

Cover:

- default model `deepseek-v4-flash` and base URL `https://api.deepseek.com`;
- only `deepseek-v4-flash` / `deepseek-v4-pro` are accepted;
- positive request timeout, max input characters, max segments, and max output tokens;
- non-negative finite retry count and temperature in `[0, 2]`;
- compatibility aliases for the copied local environment: `LLM_REQUEST_TIMEOUT`, `LLM_MAX_RETRIES`, and `LLM_TEMPERATURE`;
- first successful script is version 1, the next is version 2, and version 1 remains unchanged;
- source snapshot, parsed content JSON, Markdown, provider, model, and creation time round-trip;
- scripts from another Session are never returned.

**Step 2: Run tests and confirm failure**

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest tests\test_settings.py tests\test_script_repository.py -q -p no:cacheprovider
```

Expected: failure because Stage 5 settings and `ProcessedScriptRecord` do not exist.

**Step 3: Add runtime dependency and configuration**

Move `httpx>=0.28,<1` into runtime dependencies and regenerate `uv.lock` offline when possible. Add:

```text
DEEPSEEK_API_KEY              optional until generation is requested
DEEPSEEK_MODEL               deepseek-v4-flash | deepseek-v4-pro
DEEPSEEK_BASE_URL            default https://api.deepseek.com
DEEPSEEK_REQUEST_TIMEOUT_SECONDS / alias LLM_REQUEST_TIMEOUT
DEEPSEEK_MAX_RETRIES         / alias LLM_MAX_RETRIES
DEEPSEEK_TEMPERATURE         / alias LLM_TEMPERATURE
DEEPSEEK_MAX_INPUT_CHARS
DEEPSEEK_MAX_SEGMENTS_PER_CHUNK
DEEPSEEK_MAX_OUTPUT_TOKENS
```

Do not expose any of these through `NEXT_PUBLIC_*`.

**Step 4: Add migration and repository**

Create `processed_scripts`:

```text
id UUID text primary key
session_id foreign key sessions.id on delete cascade
provider varchar(64)
model varchar(128)
version integer
source_segment_snapshot text
content_json text
markdown_text text
created_at datetime
unique(session_id, version)
index(session_id)
```

`ProcessedScriptRepository.create()` assigns `max(version)+1`, serializes JSON deterministically with UTF-8 source text, and never updates an existing row. Provide `list_for_session()` newest-version-first and `get()`.

**Step 5: Run focused tests and migration**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_settings.py tests\test_script_repository.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
```

Expected: tests pass and migration reports `20260723_0004 (head)`.

### Task 2: Strict script schema, source-reference validation, chunking, and Markdown

**Files:**
- Create: `backend/app/text_processing/__init__.py`
- Create: `backend/app/text_processing/models.py`
- Create: `backend/app/text_processing/parser.py`
- Create: `backend/app/text_processing/chunking.py`
- Create: `backend/app/text_processing/markdown.py`
- Create: `backend/tests/test_script_parser.py`
- Create: `backend/tests/test_script_chunking.py`

**Step 1: Write failing parser and chunking tests**

Required parser cases:

1. valid JSON parses;
2. empty content raises a stable `ScriptOutputError`;
3. truncated/invalid JSON raises;
4. missing/extra fields fail Pydantic validation;
5. every `source_segment_id` belongs to the supplied Session snapshot;
6. no duplicate reference and every input segment is referenced exactly once;
7. `end_ms >= start_ms`, non-empty `clean_text`, string-only notes/warnings;
8. model-provided start/end must match the min/max source timing for its references.

Required chunking cases:

- chronological order is preserved;
- no chunk exceeds the configured segment count;
- no chunk exceeds the character budget unless one individual caption itself exceeds it;
- empty input returns no chunks;
- Unicode/newlines are counted and preserved.

Markdown must contain title, model, version, generation time, timestamp blocks, cleaned text, and source ID references.

**Step 2: Run tests and confirm failure**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_script_parser.py tests\test_script_chunking.py -q -p no:cacheprovider
```

Expected: failure because `app.text_processing` does not exist.

**Step 3: Implement immutable input/output models and strict parsing**

Use Pydantic v2 models with `extra="forbid"`:

```json
{
  "title": "string",
  "sections": [{
    "source_segment_ids": ["segment-id"],
    "start_ms": 0,
    "end_ms": 2000,
    "clean_text": "string",
    "notes": []
  }],
  "warnings": []
}
```

Store a source snapshot containing `segment_id`, revision, language, raw/display text, and normalized start/end. The parser accepts the allowed snapshot as authority and rejects foreign, missing, duplicated, or timing-inconsistent references.

**Step 4: Implement deterministic bounded chunking and Markdown rendering**

Chunk by both segment count and total input character count. Render Markdown from validated output only; escape/control title lines without altering cleaned paragraph text.

**Step 5: Run focused tests**

Expected: all parser/chunking tests pass.

### Task 3: DeepSeek adapter and retrying script service

**Files:**
- Create: `backend/app/text_processing/provider.py`
- Create: `backend/app/text_processing/deepseek_provider.py`
- Create: `backend/app/text_processing/script_service.py`
- Create: `backend/tests/test_deepseek_provider.py`
- Create: `backend/tests/test_script_service.py`

**Step 1: Write failing provider and service tests**

Use `httpx.MockTransport` and fixed provider responses. Cover:

- POST to `{base_url}/chat/completions`;
- Bearer key stays only in the request header and never in errors;
- configured model, `response_format=json_object`, explicit JSON prompt, `stream=false`, max tokens, temperature, and non-thinking mode;
- HTTP 401/403 is non-retryable configuration/auth failure;
- timeout, 429, and 5xx map to retryable `DeepSeekRequestError`;
- empty content, `finish_reason=length`, invalid JSON, and schema mismatch retry only up to the configured finite limit;
- successful multi-chunk results merge in source time order;
- a failed later chunk returns no draft result;
- the prompt instructs the model not to translate, invent facts, remove names/numbers/times, or modify source IDs.

**Step 2: Run tests and confirm failure**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_deepseek_provider.py tests\test_script_service.py -q -p no:cacheprovider
```

Expected: failure because the provider/service do not exist.

**Step 3: Implement the minimal provider boundary**

Define a small `ScriptCompletionProvider` Protocol used by the current service. `DeepSeekCompletionProvider` uses a request-scoped `httpx.AsyncClient`, explicit timeout, no streaming, sanitized exceptions, and never logs content or credentials.

**Step 4: Implement the service**

The service:

1. snapshots normalized Final segments;
2. creates bounded chunks;
3. sends each chunk to the provider;
4. retries only retryable transport/output failures;
5. strictly parses each response against that chunk;
6. merges validated sections chronologically;
7. returns one validated content object and source snapshot;
8. does not touch SQLAlchemy records or source text.

**Step 5: Run focused tests**

Expected: all provider/service tests pass with no network access.

### Task 4: Script generation, history, detail, and Markdown export API

**Files:**
- Create: `backend/app/api/scripts.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/test_scripts_api.py`

**Step 1: Write failing API tests**

Cover:

- `POST /api/sessions/{id}/scripts`;
- `GET /api/sessions/{id}/scripts`;
- `GET /api/scripts/{script_id}`;
- `GET /api/scripts/{script_id}/export`;
- missing Session/Script returns 404;
- Session with no Final returns 409 and does not call DeepSeek;
- missing DeepSeek key returns 503 only on POST;
- successful result creates version 1 and version 2 without overwriting;
- list is newest-version-first;
- detail returns parsed source snapshot/content and Markdown;
- export is UTF-8 Markdown attachment with a versioned filename;
- generation/parse failure returns sanitized `Script generation failed`, logs `session_id` and `error_type=deepseek_error`, saves no row, and leaves all `SegmentRecord` values unchanged.

**Step 2: Run test and confirm failure**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_scripts_api.py -q -p no:cacheprovider
```

Expected: failure because the routes are absent.

**Step 3: Implement routes and injectable service factory**

Build the default DeepSeek service from server-side Settings. Tests replace the app-level factory with a fake service; the browser never receives a key. Generate outside persistence writes, then insert and commit only after the entire output validates. Return metadata plus content in the POST/detail response.

**Step 4: Run API tests**

Expected: all script API tests pass.

### Task 5: Frontend processed-script workflow

**Files:**
- Modify: `frontend/types/session.ts`
- Modify: `frontend/lib/api.ts`
- Modify: `frontend/components/room-connection.tsx`
- Modify: `frontend/app/globals.css`

**Step 1: Add strict client types and API functions**

Add `ProcessedScriptSummary`, `ProcessedScriptDetail`, `ScriptSection`, and generation/list/detail/export clients. Keep URL construction in `lib/api.ts`.

**Step 2: Integrate Session history loading**

When opening or connecting to a Session, load its script versions independently of caption hydration. Changing Session clears stale selected-script state. A script-list failure must not clear or alter captions.

**Step 3: Add generation and view controls**

Add:

- “生成整理版台本” button, enabled only when a Session has Final captions;
- visible generating state and retryable error;
- original transcript / processed script toggle;
- version selector newest-first;
- provider, model, version, and generation time;
- section timestamp, cleaned text, notes, and source IDs;
- Markdown download link for the selected script.

Generation success refreshes versions and selects the new version. Generation failure keeps the original caption timeline and source exports intact.

**Step 4: Add responsive styles and run gates**

```powershell
Set-Location frontend
$env:CI='true'
pnpm typecheck
pnpm build
```

Expected: typecheck and Next.js production build pass.

### Task 6: Stage 5 vertical verification, real DeepSeek acceptance, documentation, and full regression

**Files:**
- Create: `scripts/verify_stage5_local.py`
- Create: `scripts/verify_stage5_deepseek.py`
- Modify: `README.md`
- Modify: `docs/stage-records.md`
- Modify: `docs/api-events.md`

**Step 1: Add a credential-free vertical verifier**

Use temporary SQLite and the real FastAPI routes with a fixed fake completion provider. Seed Final captions, generate twice, then verify:

- versions `[2, 1]`;
- strict source references and timing;
- detail and Markdown attachment;
- source Final rows are byte-for-byte unchanged;
- source JSON/SRT/VTT/Markdown exports remain unchanged;
- an invalid model response creates no third row;
- no secret/raw Provider field leaks;
- clean database/application shutdown.

Print one JSON line with `status=ok` and `clean_shutdown=true`.

**Step 2: Run all automated gates**

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp ..\.runtime\pytest-stage5-final
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current

Set-Location ..
.\backend\.venv\Scripts\python.exe scripts\verify_stage5_local.py

Set-Location frontend
$env:CI='true'
pnpm typecheck
pnpm build
```

Expected: every backend test passes, migration is `20260723_0004 (head)`, the local verifier reports append-only versions and unchanged source data, and frontend gates pass.

**Step 3: Run one real DeepSeek integration**

Use the local ignored `.env` only. The verifier reads a completed Session's durable Final rows, calls the configured `deepseek-v4-flash` or `deepseek-v4-pro`, validates and persists one new version, then verifies detail/export/source traceability. It must never print the key or full request headers. If the local copied model uses a deprecated alias, update only `DEEPSEEK_MODEL` in this project to `deepseek-v4-flash`.

**Step 4: Browser acceptance**

Start migrated API and frontend, open the completed Chinese Session, generate or select the new processed script, switch between original and processed views, download its Markdown, and verify the original four source exports still work. Confirm zero browser console errors and no RTC connection is required for historical processing.

**Step 5: Clean up and document**

Stop only the exact API/frontend processes created for acceptance; verify ports 8000/3000 are clear. Record exact test count, migration head, Session/script/version IDs, provider/model, source-reference result, environment differences, known limits, and Stage 6 boundary without credentials.
