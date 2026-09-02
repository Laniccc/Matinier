# Stage 3 Live Captions Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build deterministic live caption reconciliation, durable SQLite Final snapshots, reliable LiveKit event delivery, and a Next.js Stage 3 caption page.

**Architecture:** The Worker consumes normalized ASR events through an async handler, reconciles them by segment and revision, commits only Final captions to SQLite, then publishes small versioned JSON messages over reliable LiveKit data. FastAPI serves ordered Final snapshots; the browser strictly decodes and merges snapshots/events through a pure revision-aware reducer.

**Tech Stack:** Python 3.12, asyncio, Pydantic, SQLAlchemy 2, Alembic, FastAPI, LiveKit Python RTC SDK, pytest, Next.js 16, React 19, TypeScript 6, livekit-client 2.20.

---

### Task 1: Caption domain and deterministic Reconciler

**Files:**
- Create: `backend/app/captions/__init__.py`
- Create: `backend/app/captions/models.py`
- Create: `backend/app/captions/reconciler.py`
- Create: `backend/tests/test_reconciler.py`

**Step 1: Write failing model and Reconciler tests**

Cover `partial v1 -> partial v2 -> final v3`, late Partial after Final, duplicate Final, two ordered segment IDs, repeated provider event IDs/fingerprints, and invalid/missing segment IDs. Assert revisions, active/final collections, and one Final object per segment.

**Step 2: Run the focused test**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_reconciler.py -q -p no:cacheprovider`

Expected: FAIL because `app.captions` does not exist.

**Step 3: Implement immutable caption types and minimal Reconciler**

Use `CaptionStatus(StrEnum)`, a frozen/slots `CaptionEvent` dataclass, and a stateful `TranscriptReconciler`. Return `CaptionEvent | None` from `consume(ASREvent)`. Track seen provider IDs, semantic fingerprints, per-segment revision, active drafts, completed segments, and stable completion order.

**Step 4: Run the focused test again**

Expected: all Reconciler tests PASS.

**Step 5: Commit checkpoint**

Workspace note: there is no Git repository, so record the checkpoint in `docs/stage-records.md` instead of executing a commit.

### Task 2: Stable adapter segment IDs

**Files:**
- Modify: `backend/app/transcription/bailian.py`
- Modify: `backend/tests/test_bailian_provider.py`

**Step 1: Add a failing protocol test**

Feed multiple Partial updates and one Final without `sentence_id`; assert one stable generated ID through the Final, then a new ID for the next sentence. Assert the generated form uses task ID plus segment number and never text hashing.

**Step 2: Run the focused test**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_bailian_provider.py -q -p no:cacheprovider`

Expected: FAIL because current events contain `segment_id=None`.

**Step 3: Implement adapter-local generated IDs**

Maintain an active generated segment ID and incrementing segment sequence inside each Provider task. Clear the active ID after Final. Preserve an explicit Bailian `sentence_id` when present.

**Step 4: Run the test again**

Expected: all Bailian Provider tests PASS.

### Task 3: Stage 3 Segment persistence and migration

**Files:**
- Modify: `backend/app/persistence/models.py`
- Modify: `backend/app/persistence/database.py`
- Create: `backend/app/persistence/segments.py`
- Create: `backend/alembic/versions/20260722_0002_create_segments.py`
- Create: `backend/tests/test_segment_repository.py`

**Step 1: Write failing repository tests**

Test Final insert, same-revision no-op, lower-revision no-op, higher-revision update, two Sessions with the same provider segment ID, Final-only validation, and audio-time ordering.

**Step 2: Run the focused test**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_segment_repository.py -q -p no:cacheprovider`

Expected: FAIL because `SegmentRecord` and repository do not exist.

**Step 3: Add the future-compatible table and repository**

Add an internal UUID primary key and a unique constraint on `(session_id, segment_id)`. Implement `upsert_final`, `list_final`, and Session status updates. Configure SQLite foreign keys and a finite busy timeout so API reads and Worker writes coexist.

**Step 4: Add and exercise Alembic migration**

Run: `cd backend; .\.venv\Scripts\python.exe -m alembic upgrade head`

Expected: migration `20260722_0002` completes and preserves existing Session rows.

**Step 5: Run repository tests**

Expected: all repository tests PASS.

### Task 4: Final snapshot API

**Files:**
- Modify: `backend/app/api/sessions.py`
- Create: `backend/tests/test_segments_api.py`

**Step 1: Write failing endpoint tests**

Cover existing Session with no segments (`[]`), missing Session (404), ordered Final rows, and exclusion of non-Final rows.

**Step 2: Run the focused test**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_segments_api.py -q -p no:cacheprovider`

Expected: FAIL with endpoint 404/absent response schema.

**Step 3: Add response schema and endpoint**

Implement `GET /api/sessions/{session_id}/segments` using the repository and a typed Pydantic response. Do not expose provider raw payloads or credentials.

**Step 4: Run endpoint tests**

Expected: all snapshot API tests PASS.

### Task 5: Reliable event schema and publisher

**Files:**
- Create: `backend/app/captions/events.py`
- Create: `backend/app/captions/publisher.py`
- Create: `backend/tests/test_caption_publisher.py`

**Step 1: Write failing publisher tests**

Use a fake LocalParticipant. Assert exact topic `livecaption.events.v1`, `reliable=True`, UTF-8 JSON, schema version, known event types, Session ID, normalized caption payload, bounded message size, metrics/status/progress/error messages, and no secret fields.

**Step 2: Run the focused test**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_caption_publisher.py -q -p no:cacheprovider`

Expected: FAIL because publisher modules do not exist.

**Step 3: Implement Pydantic envelopes and LiveKit publisher**

Serialize with compact separators and `ensure_ascii=False`. Reject oversized payloads before calling LiveKit. Keep a protocol/interface so tests and later transports can inject alternatives.

**Step 4: Run publisher tests**

Expected: all publisher tests PASS.

### Task 6: Transcription event hook and Worker caption runtime

**Files:**
- Modify: `backend/app/transcription/session.py`
- Modify: `backend/tests/test_transcription_session.py`
- Create: `backend/app/captions/runtime.py`
- Create: `backend/tests/test_caption_runtime.py`
- Modify: `backend/app/worker/entrypoint.py`
- Modify: `backend/tests/test_worker_entrypoint.py`

**Step 1: Write failing event-hook tests**

Assert each normalized ASR event is awaited exactly once, handler failure terminates explicitly, metrics remain accurate, and cancellation still closes all tasks.

**Step 2: Implement the async event handler**

Add an optional `Callable[[ASREvent], Awaitable[None]]` to `TranscriptionSession`; call it after metrics observation and before moving to the next Provider event.

**Step 3: Write failing runtime tests**

Use fake publisher/repository to assert: running status, Partial publish without persistence, Final persist-before-publish, progress publishing, metrics then completed, failed/error publishing, idempotent close, and redacted user-visible error messages.

**Step 4: Implement caption runtime**

Own one Reconciler, repository, and publisher per replay Track. Keep database and LiveKit failures explicit. Update Session timestamps/status through the repository.

**Step 5: Wire runtime into Worker**

Pass `runtime.handle_asr_event` to the transcription factory, publish progress every 25 frames, publish completion after `finish()` returns metrics, publish failure before cleanup, dispose Worker-side database resources, and retain all Stage 2 shutdown guarantees.

**Step 6: Run focused tests**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_transcription_session.py tests\test_caption_runtime.py tests\test_worker_entrypoint.py -q -p no:cacheprovider`

Expected: all focused tests PASS without unclosed-task warnings.

### Task 7: Strict browser decoder and revision-aware store

**Files:**
- Create: `frontend/types/captions.ts`
- Create: `frontend/lib/captions.ts`
- Modify: `frontend/lib/api.ts`
- Modify: `frontend/types/session.ts`

**Step 1: Define discriminated TypeScript types and runtime validation**

Implement a decoder that accepts only schema version 1, exact known `topic`/`type` combinations, the active Session ID, and valid payload primitives. Return `null` for unknown/malformed packets.

**Step 2: Implement the pure reducer**

Maintain `sessionStatus`, draft/final maps, `lastRevisionBySegment`, `error`, metrics, and current audio time. Snapshot hydrate and event merge both reject stale revisions. Final removes its draft; repeated Final is idempotent; ordered selectors sort by audio time.

**Step 3: Add Final snapshot API client**

Implement `getSegments(sessionId)` and matching response types.

**Step 4: Run type checking**

Run: `cd frontend; pnpm typecheck`

Expected: PASS.

### Task 8: Stage 3 live caption page

**Files:**
- Modify: `frontend/components/room-connection.tsx`
- Modify: `frontend/app/globals.css`

**Step 1: Wire snapshot and LiveKit data handling**

Fetch the initial Final snapshot, register `RoomEvent.DataReceived`, connect, then refresh/merge the snapshot once to close the race. Decode UTF-8 only for the exact LiveKit topic. Preserve completed status when the Agent leaves normally.

**Step 2: Build the Stage 3 UI**

Show filename, Session status, formatted audio time, light Draft captions, ordered Final timeline, start/end times, latency metrics, and visible errors. Scroll the newest Final into view and keep the existing Session/create/join controls usable.

**Step 3: Add responsive styles**

Use the existing dark panel system, lighter/italic Draft treatment, normal Final cards, accessible live regions, and mobile stacking. Do not introduce a design-system dependency.

**Step 4: Run frontend gates**

Run: `cd frontend; pnpm typecheck; pnpm build`

Expected: typecheck PASS and Next.js production build PASS.

### Task 9: Full regression and real Stage 3 acceptance

**Files:**
- Create: `scripts/verify_stage3_local.py`
- Modify: `README.md`
- Modify: `docs/stage-records.md`
- Modify: `docs/api-events.md` if present; otherwise create it

**Step 1: Add a credential-free Stage 3 verifier**

Use normalized fake ASR events, Reconciler, repository, and fake publisher to verify revision behavior, one durable Final, snapshot recovery, completed status, metrics, and clean shutdown in one JSON result.

**Step 2: Run all backend tests and migration**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp .pytest-tmp\stage3-final; .\.venv\Scripts\python.exe -m alembic upgrade head`

Expected: all tests PASS and migration reaches head.

**Step 3: Run local verifier and frontend gates**

Run: `.\backend\.venv\Scripts\python.exe scripts\verify_stage3_local.py`

Expected: JSON reports Partial replacement, one Final, snapshot recovery, completed state, no error, and `clean_shutdown=true`.

Run: `cd frontend; pnpm typecheck; pnpm build`

Expected: PASS.

**Step 4: Run real end-to-end acceptance**

Start LiveKit, migrated API, Worker, and frontend. Open/create a file Session, run the existing Chinese speech Replay using its Session ID, and verify Partial changes in place, one Final, completed state, metrics, and visible timing. Refresh/rejoin the same Session and verify Final recovery from SQLite. Send/observe an unknown packet and ensure the page remains stable.

**Step 5: Clean up and document**

Stop all API/Worker/Frontend/LiveKit processes and verify there are no orphan Python, FFmpeg, or Node processes created by the test. Record exact test counts, Session ID, caption counts, known environment differences, and Stage 4 boundary without including credentials.
