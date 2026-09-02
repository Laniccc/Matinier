# Stage 4 Timeline, History, and Exports Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Turn durable Final captions into a restart-safe session history, normalized timeline, and deterministic JSON/SRT/WebVTT/Markdown downloads.

**Architecture:** Keep Stage 3's real-time `livecaption.events.v1` contract unchanged. The Worker continues to own Final writes and additionally persists the actual ASR provider/model and terminal metrics; FastAPI reads only durable Final rows, normalizes their audio timestamps in a pure timeline service, and passes that snapshot to format-specific exporters. The frontend reopens completed sessions through HTTP without requiring a LiveKit connection and exposes direct download links.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, SQLite, pytest, Next.js 16, React 19, TypeScript 6, pnpm.

**Scope boundary:** No DeepSeek, translation, Draft persistence, uploads, complex manual editor, or Stage 6 expanded status machine. This directory is not a Git repository, so commit steps are intentionally omitted.

---

### Task 1: Persist export metadata and terminal metrics

**Files:**
- Modify: `backend/app/persistence/models.py`
- Modify: `backend/app/persistence/segments.py`
- Create: `backend/alembic/versions/20260722_0003_stage4_export_metadata.py`
- Modify: `backend/app/captions/runtime.py`
- Modify: `backend/app/worker/entrypoint.py`
- Modify: `backend/tests/test_segment_repository.py`
- Modify: `backend/tests/test_caption_runtime.py`
- Modify: `backend/tests/test_worker_entrypoint.py`

**Step 1: Write failing persistence tests**

Assert that a Final stores `received_at_ms`, that running stores the actual provider/model, and that completion atomically stores status plus all `TranscriptionMetrics` fields. Assert a failed Session does not invent successful metrics.

**Step 2: Run the focused tests**

Run:

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest tests\test_segment_repository.py tests\test_caption_runtime.py tests\test_worker_entrypoint.py -q -p no:cacheprovider
```

Expected: FAIL because the Stage 4 columns and repository methods do not exist.

**Step 3: Add schema and migration**

Add nullable `SessionRecord` columns:

```python
asr_provider: Mapped[str | None]
asr_model: Mapped[str | None]
final_result_count: Mapped[int | None]
first_partial_latency_ms: Mapped[float | None]
average_final_latency_ms: Mapped[float | None]
provider_error_count: Mapped[int | None]
sent_audio_chunk_count: Mapped[int | None]
sent_audio_bytes: Mapped[int | None]
```

Add non-null `SegmentRecord.received_at_ms`, backfilling existing rows from `finalized_at` in migration `20260722_0003`. Existing Stage 3 Session and Segment rows must survive upgrade.

**Step 4: Persist real runtime metadata**

Extend the Worker runtime factory with `provider_name` and `model_name`; pass `"bailian"` and `settings.bailian_asr_model` from the Worker entrypoint. Add repository operations that mark a Session running with provider/model and complete it with all metrics in the same transaction. Keep Final-before-live-publish ordering unchanged.

**Step 5: Run tests and migration**

Run:

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest tests\test_segment_repository.py tests\test_caption_runtime.py tests\test_worker_entrypoint.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
```

Expected: focused tests PASS and Alembic reports `20260722_0003 (head)`.

### Task 2: Pure timeline normalization

**Files:**
- Create: `backend/app/timeline/__init__.py`
- Create: `backend/app/timeline/models.py`
- Create: `backend/app/timeline/service.py`
- Create: `backend/tests/test_timeline_service.py`

**Step 1: Write failing normalization tests**

Cover audio-time ordering, negative timestamps, `end < start`, missing start, missing end with a following cue, missing final end, stable ordering ties, allowed overlap, empty input, and preservation of `display_text` including Unicode/newlines.

**Step 2: Run the focused test**

Run:

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest tests\test_timeline_service.py -q -p no:cacheprovider
```

Expected: FAIL because `app.timeline` does not exist.

**Step 3: Implement the pure service**

Create immutable timeline items containing identifiers, revision, language, raw/display text, normalized start/end, confidence, received/finalized timestamps. Apply these explicit rules:

```text
sort by known audio_start_ms, then audio_end_ms, created_at, id
start = max(0, audio_start_ms); missing start = previous normalized end or 0
end < start = start
missing end = next known non-negative start when it is after start
otherwise missing end = start + 2000 ms
do not remove legitimate overlap and do not alter caption text
```

Expose total duration as the maximum normalized end.

**Step 4: Run the test**

Expected: all timeline tests PASS.

### Task 3: Deterministic exporters

**Files:**
- Create: `backend/app/export/__init__.py`
- Create: `backend/app/export/models.py`
- Create: `backend/app/export/json_exporter.py`
- Create: `backend/app/export/srt_exporter.py`
- Create: `backend/app/export/vtt_exporter.py`
- Create: `backend/app/export/markdown_exporter.py`
- Create: `backend/tests/test_exporters.py`

**Step 1: Write failing exporter tests**

Assert JSON parses and contains Session/source/provider/model/metrics/Final segments without secrets or Provider raw payloads; SRT uses `HH:MM:SS,mmm`; VTT starts with `WEBVTT` and uses `HH:MM:SS.mmm`; Markdown contains title/source/language/duration and timestamp blocks. Assert audio ordering, empty-session behavior, Unicode, multiline text, cue-safe special characters, and byte-for-byte repeatability.

**Step 2: Run the test**

Run:

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest tests\test_exporters.py -q -p no:cacheprovider
```

Expected: FAIL because exporters do not exist.

**Step 3: Implement exporters**

Use one normalized timeline snapshot as the only exporter input. JSON uses UTF-8, `ensure_ascii=False`, two-space indentation, no generated-at timestamp, and a trailing newline. SRT/VTT use 1-based cues; VTT protects cue text containing the timing arrow without visually rewriting ordinary text. Empty results are: valid JSON metadata with `segments=[]`, empty SRT, `WEBVTT\n\n`, and a Markdown header plus an explicit no-caption message.

**Step 4: Run the test**

Expected: all exporter tests PASS and repeated calls return identical bytes.

### Task 4: Session history and export API

**Files:**
- Modify: `backend/app/api/sessions.py`
- Create: `backend/app/api/exports.py`
- Modify: `backend/app/main.py`
- Modify: `backend/tests/test_sessions_api.py`
- Create: `backend/tests/test_exports_api.py`

**Step 1: Write failing API tests**

Cover `GET /api/sessions` newest-first history, extended persisted metadata/metrics in Session responses, all four export query formats, exact media types and attachment filenames, missing Session 404, invalid format 422, empty Session outputs, Final-only behavior, stable repeated downloads, and sanitized internal export errors.

**Step 2: Run API tests**

Run:

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest tests\test_sessions_api.py tests\test_exports_api.py -q -p no:cacheprovider
```

Expected: FAIL because history and export routes are absent.

**Step 3: Implement the routes**

Add `GET /api/sessions` ordered by `created_at DESC, id DESC`. Add:

```text
GET /api/sessions/{session_id}/export?format=json
GET /api/sessions/{session_id}/export?format=srt
GET /api/sessions/{session_id}/export?format=vtt
GET /api/sessions/{session_id}/export?format=markdown
```

Return filenames `transcript.json`, `source.srt`, `source.vtt`, and `script.md`; set UTF-8 media types and `Content-Disposition: attachment`. Query only `SegmentRepository.list_final()`, normalize once, and map unexpected failures to HTTP 500 detail `Export failed` while logging `session_id`, format, and `error_type=export_error` without response internals.

**Step 4: Run API tests**

Expected: all history/export API tests PASS.

### Task 5: Frontend history and export panel

**Files:**
- Modify: `frontend/types/session.ts`
- Modify: `frontend/lib/api.ts`
- Modify: `frontend/components/room-connection.tsx`
- Modify: `frontend/app/globals.css`

**Step 1: Add typed clients**

Extend `Session` with nullable provider/model/metric fields. Add `listSessions()` and `getSessionExportUrl(sessionId, format)`. Keep URL building in `lib/api.ts` so components do not duplicate API knowledge.

**Step 2: Add restart-safe history opening**

Load history when the page mounts and via a refresh button. Selecting a history item must disconnect any active Room, fetch Session plus Final snapshot, hydrate the existing caption reducer, and render the timeline without requesting a LiveKit token or connecting RTC.

**Step 3: Add download controls**

Show JSON/SRT/VTT/Markdown attachment links for the selected Session. Render persisted metrics after historical reopen, accessible loading/error states, source/status/date, and an empty-history message. Preserve Stage 3 create/join/realtime behavior.

**Step 4: Add responsive styles and run gates**

Run:

```powershell
Set-Location frontend
$env:CI='true'
pnpm typecheck
pnpm build
```

Expected: typecheck and Next.js production build PASS.

### Task 6: Stage 4 verification, documentation, and full regression

**Files:**
- Create: `scripts/verify_stage4_local.py`
- Modify: `README.md`
- Modify: `docs/stage-records.md`
- Modify: `docs/api-events.md`

**Step 1: Add a credential-free vertical verifier**

Create a temporary SQLite database, Session, out-of-order Final rows and one Draft sentinel. Exercise the actual FastAPI history, snapshot, and four export endpoints. Verify ordering, normalization, Final-only behavior, deterministic repeated downloads, content types/filenames, metadata/metrics, special characters, and no secret/raw payload fields. Print one JSON line ending with `status=ok` and `clean_shutdown=true`.

**Step 2: Run full automated gates**

Run:

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp ..\.runtime\pytest-stage4-final
.\.venv\Scripts\python.exe -m alembic upgrade head

Set-Location ..
.\backend\.venv\Scripts\python.exe scripts\verify_stage4_local.py

Set-Location frontend
$env:CI='true'
pnpm typecheck
pnpm build
```

Expected: every backend test passes, migration is at head, the verifier reports all four formats and clean shutdown, and frontend gates pass.

**Step 3: Run persisted real-data acceptance**

Start only migrated API and frontend, reopen the completed Stage 3 Session, confirm its Final timeline survives restart, download all four formats twice, and compare hashes. Validate JSON parses, SRT/VTT timing, Markdown content, and equality with the page Final text. No Bailian call is needed because Stage 4 consumes durable Final rows.

**Step 4: Clean up and document**

Stop API/frontend, verify ports 8000/3000 and created Python/Node processes are gone, and record exact test count, migration head, Session ID, export hashes, environment differences, known limits, and the Stage 5 DeepSeek boundary. Do not include credentials.
