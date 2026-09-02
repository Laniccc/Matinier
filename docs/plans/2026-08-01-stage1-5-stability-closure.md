# Stage 1.5 Stability Closure Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Freeze Stage 1 after proving that the existing caption pipeline starts, stops, survives expected failures, persists authoritative Final captions, and is ready for single-instance containerization.

**Architecture:** Preserve the existing LiveKit data plane for caption events. Add a small authenticated Worker-to-API control plane for source aborts, separate persisted business state from resource state, centralize SQLite and data-directory configuration, and add fake/transport-only diagnostics around the current provider and reconciler boundaries.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, LiveKit Agents/RTC, FFmpeg, httpx, pytest, Next.js/TypeScript, SQLite WAL.

---

## Execution rules

- Execute phases A through F in order and stop for a stage report after each phase.
- Run focused tests first, then the smallest relevant regression suite and one executable use case.
- Keep Partial/Final LiveKit publishing out of HTTP; internal HTTP is control-plane only.
- Do not add providers, input source types, authentication systems, or deployment manifests in Stage 1.5.
- The repository contains a placeholder `.git` directory rather than usable Git metadata, so work is performed in place and no commit commands are claimed.

## Phase A — Orphan HLS repair and resource lifecycle separation

### Task A1: Persist source cleanup state

**Files:**
- Create: `backend/alembic/versions/20260801_0009_add_source_lifecycle.py`
- Modify: `backend/app/persistence/models.py`
- Modify: `backend/app/persistence/sessions.py`
- Modify: `backend/app/api/sessions.py`
- Test: `backend/tests/test_session_repository.py`

1. Add failing repository tests for default `source_status`, idempotent stopping/stopped transitions, and cleanup detail preservation.
2. Add `source_status`, `source_ended_at`, `cleanup_status`, and `cleanup_detail` columns with safe defaults for existing rows.
3. Add repository methods that update resource state without attempting a Session business-state transition.
4. Run `backend/.venv/Scripts/python.exe -m pytest tests/test_session_repository.py -q` from `backend`; expect all tests to pass.

### Task A2: Make HLS cleanup exhaustive and idempotent

**Files:**
- Modify: `backend/app/hls/manager.py`
- Modify: `backend/app/hls/source.py`
- Modify: `backend/app/hls/decoder.py`
- Test: `backend/tests/test_hls_input.py`

1. Add failing tests for repeated stop, stopped FFmpeg, already-unpublished Track, disconnected Room, and continuation after an individual cleanup failure.
2. Return a structured cleanup result from Manager stop/abort while keeping repeated calls successful.
3. Retain stopped request outcomes long enough to distinguish `already_stopped` from unknown sessions.
4. Refactor `HLSLiveSource` cleanup into independent guarded steps: decoder, queue, Track, AudioSource, Room.
5. Update source state to `starting`, `running`, `stopping`, `stopped`, or `failed` via callbacks owned by the API process.
6. Run `backend/.venv/Scripts/python.exe -m pytest tests/test_hls_input.py -q`; expect all tests to pass.

### Task A3: Add authenticated internal abort control plane

**Files:**
- Create: `backend/app/api/internal.py`
- Create: `backend/app/worker/control.py`
- Modify: `backend/app/settings.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/worker/entrypoint.py`
- Test: `backend/tests/test_internal_control.py`
- Test: `backend/tests/test_worker_entrypoint.py`

1. Add failing API tests for token rejection, successful abort, terminal-Session cleanup, and duplicate request idempotency.
2. Add `POST /internal/sessions/{session_id}/source/abort` using `X-Internal-Control-Token` and bounded public response details.
3. Add an httpx-based Worker client with a short timeout, generated request id, and structured correlation logging.
4. Invoke abort after the original main-chain failure is persisted; do not invoke it for translation-only degradation or normal source EOF.
5. Ensure abort failure is logged as cleanup pending and never replaces the original ASR error.
6. Run focused internal-control and Worker tests; expect all tests to pass.

### Task A4: Migrate and manually exercise the failure cleanup path

**Files:**
- Modify: `docs/stage-records.md`
- Modify: `.env.example`
- Modify: `README.md`

1. Stop the controlled development stack, back up the current SQLite file, and run `alembic upgrade head`.
2. Start the stack and create one HLS Session or deterministic manager harness.
3. Inject a main ASR failure and confirm Session failure is retained while source cleanup reaches stopped.
4. Repeat stop/abort and confirm success, then inspect FFmpeg processes, LiveKit participants, Worker tasks, and SQLite state.
5. Record exact commands, results, modified files, and known limitations in `docs/stage-records.md`.

## Phase B — SQLite concurrency, uniqueness, and absolute path hardening

### Task B1: Resolve one absolute data directory and database URL

**Files:**
- Modify: `backend/app/settings.py`
- Modify: `backend/app/main.py`
- Modify: `backend/alembic/env.py`
- Modify: `scripts/start_dev.ps1`
- Modify: `scripts/start_dev.sh`
- Modify: `.env.example`
- Modify: `.gitignore`
- Test: `backend/tests/test_settings.py`

1. Add tests that change working directory and still resolve the same database path.
2. Add `DATA_DIR` and an absolute normalized SQLite URL constructed in the shared Settings class.
3. Create the configured directory explicitly and log the redacted resolved database path in API and Worker startup.
4. Preserve `backend/live_caption.db` by making a verified backup and moving it to `backend/data/live_caption.db` only after services stop.

### Task B2: Apply SQLite connection policy once per connection

**Files:**
- Modify: `backend/app/persistence/database.py`
- Test: `backend/tests/test_settings.py`
- Test: `backend/tests/test_session_repository.py`

1. Test `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, and `busy_timeout=5000` on a file database.
2. Apply PRAGMAs in one SQLAlchemy connection event used by API, Worker, and test harnesses.
3. Log WAL mode at startup and warn clearly if WAL cannot be enabled.

### Task B3: Audit Final transaction boundaries and idempotency

**Files:**
- Modify if required: `backend/app/persistence/segments.py`
- Modify if required: `backend/app/persistence/translations.py`
- Modify if required: `backend/app/captions/runtime.py`
- Modify if required: `backend/app/translation/runtime.py`
- Create only if schema correction is required: `backend/alembic/versions/20260801_0010_final_idempotency.py`
- Test: `backend/tests/test_segment_repository.py`
- Test: `backend/tests/test_translation_repository.py`
- Test: `backend/tests/test_caption_runtime.py`
- Test: `backend/tests/test_translation_runtime.py`

1. Prove duplicate Final updates rather than inserts, old revisions cannot overwrite new revisions, and old Draft cannot overwrite Final.
2. Verify commit completes before the reliable LiveKit Final publish in both source and translation runtimes.
3. Inspect existing duplicates before any constraint migration; never silently delete history.

### Task B4: Run concurrent SQLite use case

**Files:**
- Modify: `docs/stage-records.md`
- Modify: `README.md`

1. Upgrade the migrated copy and start API plus Worker from different working directories.
2. Concurrently read history, persist Finals, save a processed script, and export subtitles.
3. Confirm one database location, WAL/SHM sidecars, persistence after restart, and no observed lock errors.

## Phase C — Low-cost long-run modes and fault injection

### Task C1: Add transport-only runtime

**Files:**
- Create: `backend/app/worker/modes.py`
- Create: `backend/app/worker/transport_runtime.py`
- Modify: `backend/app/settings.py`
- Modify: `backend/app/worker/entrypoint.py`
- Modify: `backend/app/worker/audio_stats.py`
- Test: `backend/tests/test_transport_runtime.py`

1. Add a non-default mode selector and frame sink that consumes the existing AudioStream without Bailian.
2. Capture frames, bytes, audio/wall durations, realtime ratio, queue watermarks, drops, process memory, and active tasks.
3. Keep normal source/Room cleanup identical to production mode.

### Task C2: Add fake ASR and translation providers

**Files:**
- Create: `backend/app/transcription/fake.py`
- Create: `backend/app/translation/fake.py`
- Modify: `backend/app/worker/entrypoint.py`
- Test: `backend/tests/test_fake_providers.py`
- Test: `backend/tests/test_stage1_5_fake_pipeline.py`

1. Implement current Provider protocols and emit partial v1, partial v2, and final v3 through existing sessions and reconcilers.
2. Add deterministic options for duplicate Final, stale Partial, translation failure, ASR failure, slow consumer, and timeout.
3. Verify SQLite and LiveKit event paths are not bypassed.

### Task C3: Add bounded long-run runner and reports

**Files:**
- Create: `scripts/run_stage1_5_longrun.py`
- Create: `backend/app/diagnostics/longrun.py`
- Modify: `.gitignore`
- Modify: `README.md`
- Test: `backend/tests/test_stage1_5_longrun.py`

1. Add CLI modes `transport-only`, `fake-provider`, and `real-provider-smoke` with explicit duration/source options.
2. Write JSON and Markdown reports under the configured reports directory with every taskbook field.
3. Add bounded failure-injection flags and refuse unbounded real-provider runs.

### Task C4: Execute low-cost long tests

**Files:**
- Generate: `backend/data/reports/stage1_5_longrun_<timestamp>.json`
- Generate: `backend/data/reports/stage1_5_longrun_<timestamp>.md`
- Modify: `docs/stage-records.md`

1. Run a short preflight, then a 30-minute local transport-only loop.
2. Run a deterministic Fake Provider long test with duplicate/stale events and one failure cleanup scenario.
3. Inspect memory trend, queues, tasks, Room, provider, and FFmpeg residue and record actual outcomes.
4. Defer any real Bailian smoke requiring user-authorized cloud use to the explicit Stage C acceptance step.

## Phase D — State model, runtime diagnostics, and structured logs

### Task D1: Normalize persisted lifecycle fields

**Files:**
- Create: `backend/alembic/versions/20260801_0011_normalize_runtime_state.py`
- Modify: `backend/app/persistence/models.py`
- Modify: `backend/app/persistence/sessions.py`
- Modify: `backend/app/sessions/state.py`
- Modify: `backend/app/api/sessions.py`
- Test: `backend/tests/test_session_state.py`
- Test: `backend/tests/test_session_repository.py`

1. Normalize Session to created/starting/running/finalizing/completed/failed/cancelled and Translation to disabled/starting/running/completed/failed.
2. Persist stop/failure/end/cleanup fields while migrating old state values explicitly.
3. Preserve rule that translation failure is degradation, not Session failure.

### Task D2: Add lightweight runtime snapshot registry and API

**Files:**
- Create: `backend/app/diagnostics/runtime.py`
- Modify: `backend/app/hls/manager.py`
- Modify: `backend/app/worker/entrypoint.py`
- Modify: `backend/app/api/sessions.py`
- Test: `backend/tests/test_runtime_api.py`

1. Aggregate persisted state with current API-owned HLS diagnostics and Worker-published lightweight snapshots.
2. Add `GET /api/sessions/{session_id}/runtime` with explicit unavailable fields and no secrets/tracebacks.

### Task D3: Add the minimal frontend runtime panel

**Files:**
- Modify: `frontend/lib/api.ts`
- Modify: `frontend/types/session.ts`
- Modify: `frontend/components/room-studio.tsx`
- Modify: `frontend/app/globals.css`

1. Add a collapsible status section for Session, Source, ASR, Translation, FFmpeg, LiveKit, counters, last event, and failure code.
2. Poll only while a run is active and display unavailable/degraded states explicitly.
3. Run `pnpm exec tsc --noEmit` and `pnpm build`; expect success.

### Task D4: Complete lifecycle logging and acceptance

**Files:**
- Modify: `backend/app/logging.py`
- Modify relevant lifecycle emitters under `backend/app/`
- Modify: `docs/stage-records.md`

1. Add stable structured fields required by the taskbook without logging secrets or URL query strings.
2. Execute one success, one translation degradation, and one source failure; reconstruct each by `session_id` from logs.

## Phase E — Export, multilingual, and remote URL safety closure

### Task E1: Audit authoritative Final exports and running markers

**Files:**
- Modify: `backend/app/api/exports.py`
- Modify: `backend/app/export/models.py`
- Modify exporter modules under `backend/app/export/`
- Test: `backend/tests/test_exporters.py`
- Test: `backend/tests/test_exports_api.py`

1. Prove every formal export reads SQLite Final records.
2. Mark running exports with `session_status`, `exported_at`, and `partial_export=true` using format-appropriate metadata.
3. Normalize negative/missing/end-before-start boundaries and test empty Sessions.

### Task E2: Prove multilingual encoding

**Files:**
- Test: `backend/tests/test_exporters.py`

1. Round-trip Chinese, English, Japanese, Arabic, mixed text, emoji, punctuation, and RTL through JSON/Markdown/SRT/VTT as UTF-8.
2. Verify SRT comma milliseconds and VTT dot milliseconds.

### Task E3: Complete RemoteMediaUrlValidator

**Files:**
- Refactor: `backend/app/hls/url_policy.py`
- Modify: `backend/app/api/rooms.py`
- Modify: `backend/app/hls/decoder.py`
- Test: `backend/tests/test_hls_input.py`

1. Expose normalized URL, resolved IPs, decision, and reason from an independent validator.
2. Enforce HTTP(S), public DNS/IP, redirect revalidation/count, connection/read timeouts, and maximum stream duration.
3. Keep FFmpeg argv-only with `shell=False` semantics and redact URL query strings in logs.
4. Execute the required public/private/metadata/redirect/shell-character cases.

### Task E4: Run export and URL acceptance

**Files:**
- Modify: `docs/stage-records.md`

1. Export a persisted multilingual Session in every supported format and reload each artifact.
2. Validate one public M3U8 and all required rejected targets, then inspect FFmpeg invocation and residue.

## Phase F — Deployment reservation and Stage 1 freeze

### Task F1: Consolidate configuration and persistent directories

**Files:**
- Modify: `backend/app/settings.py`
- Modify: `.env.example`
- Modify: `frontend/.env.local.example`
- Modify: `scripts/start_dev.ps1`
- Modify: `scripts/start_dev.sh`
- Test: `backend/tests/test_settings.py`
- Test: `backend/tests/test_dev_launchers.py`

1. Add and validate every taskbook configuration key while keeping localhost development defaults separate from secrets.
2. Route database, exports, logs, and reports under `DATA_DIR` and document Windows/Linux mappings.

### Task F2: Add liveness/readiness and Worker health

**Files:**
- Modify: `backend/app/api/health.py`
- Create: `backend/app/worker/health.py`
- Modify: `backend/app/worker/entrypoint.py`
- Test: `backend/tests/test_health.py`
- Test: `backend/tests/test_worker_health.py`

1. Add `/health/live` and readiness checks for Settings, SQLite read/write, LiveKit config, and directories without calling Bailian.
2. Expose lightweight Worker alive/LiveKit/jobs/last-event health on its existing internal boundary.

### Task F3: Freeze documentation and remove dead code

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/stage-records.md`
- Modify: `.gitignore`

1. Document actual process boundaries, local startup, data, providers, test modes, long tests, injection, exports, limits, and Stage 2 entry.
2. Remove code proven obsolete by A–F and verify no parallel runtime was introduced.

### Task F4: Final acceptance matrix

**Files:**
- Create: `reports/stage1_5_freeze_<timestamp>.md` or configured data equivalent
- Modify: `docs/stage-records.md`

1. Run backend focused/full tests, frontend type/build checks, migrations, and local verification scripts.
2. Exercise microphone, local media, and public M3U8; verify source and translation modes, refresh recovery, exports, abort cleanup, health endpoints, and no residual resources.
3. Record each of the 20 freeze criteria as passed, failed, or blocked with evidence; mark Stage 1 Frozen only if all mandatory criteria actually pass.
