# Stage 6 Engineering Closure Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Consolidate Stages 0–5 into a stable, demonstrable first release with the required session state machine, explicit timeouts/errors, one-command development launchers, a credential-free integration test, and complete operational documentation.

**Architecture:** Preserve the existing audio, ASR, caption, persistence, export, and DeepSeek boundaries. Add one authoritative session-state domain and repository used by API and Worker, persist user-visible terminal errors, and publish the same states through `livecaption.events.v1`. Development launchers supervise the three application processes without owning Replay; automated verification remains cloud-free while the existing manual E2E scripts cover real services.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, SQLite, asyncio, LiveKit Agents/RTC, PowerShell, POSIX shell, pytest, Next.js 16, React 19, TypeScript 6.

**Repository note:** `git status` reports that this directory is not a Git repository. Worktree and commit steps from the generic planning skill are therefore intentionally omitted; every batch is instead closed by focused tests and recorded in `docs/stage-records.md`.

---

### Task 1: Authoritative session state machine and durable error state

**Files:**
- Create: `backend/app/sessions/__init__.py`
- Create: `backend/app/sessions/state.py`
- Create: `backend/app/persistence/sessions.py`
- Modify: `backend/app/persistence/models.py`
- Modify: `backend/app/persistence/segments.py`
- Create: `backend/alembic/versions/20260728_0005_add_session_state_errors.py`
- Modify: `backend/app/api/sessions.py`
- Modify: `backend/app/api/livekit_token.py`
- Modify: `backend/app/captions/events.py`
- Test: `backend/tests/test_session_state.py`
- Test: `backend/tests/test_session_repository.py`
- Modify: `backend/tests/test_sessions_api.py`
- Modify: `backend/tests/test_livekit_token.py`

**Step 1: Write failing state tests**

Test the exact normal chain:

```text
created -> room_ready -> replaying -> transcribing -> finalizing -> completed
```

Also test that every non-terminal state may move to `failed` or `cancelled`, identical transitions are idempotent, terminal states cannot change, and skipping/regressing normal states raises `InvalidSessionTransition`.

**Step 2: Run the focused tests and confirm failure**

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest tests\test_session_state.py tests\test_session_repository.py -q -p no:cacheprovider
```

Expected: collection fails because the session state module/repository do not exist.

**Step 3: Implement the domain and repository**

Define a single `SessionStatus` literal/enum and `transition_session_status(current, target)`. Add a `SessionRepository` that:

- creates/lists/gets sessions;
- applies only domain-approved transitions;
- sets `started_at` when replay begins;
- sets `ended_at` only for `completed`, `failed`, or `cancelled`;
- stores sanitized `error_code` / `error_message` only for failure or cancellation;
- stores Provider/model at `transcribing`;
- stores terminal metrics at `completed`.

Move Session status responsibilities out of `SegmentRepository`; it remains responsible only for Final segments.

**Step 4: Add migration and API integration**

Migration `20260728_0005` adds nullable `error_code` and `error_message`, and maps legacy `running` rows to `transcribing`. Session responses expose the fields. Issuing the first valid LiveKit token advances `created -> room_ready` but repeated token requests never regress later or terminal sessions.

**Step 5: Run focused tests and migration**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_session_state.py tests\test_session_repository.py tests\test_sessions_api.py tests\test_livekit_token.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
```

Expected: tests pass and Alembic reports `20260728_0005 (head)`.

### Task 2: Worker lifecycle states and browser synchronization

**Files:**
- Modify: `backend/app/captions/runtime.py`
- Modify: `backend/app/captions/publisher.py`
- Modify: `backend/app/worker/entrypoint.py`
- Modify: `frontend/types/session.ts`
- Modify: `frontend/types/captions.ts`
- Modify: `frontend/lib/captions.ts`
- Modify: `frontend/components/room-connection.tsx`
- Modify: `frontend/app/globals.css`
- Modify: `backend/tests/test_caption_runtime.py`
- Modify: `backend/tests/test_caption_publisher.py`
- Modify: `backend/tests/test_worker_entrypoint.py`

**Step 1: Update failing runtime/event tests**

Require the Worker sequence:

```text
start_replay() -> replaying
STREAM_STARTED -> transcribing
begin_finalizing() -> finalizing
complete(metrics) -> completed
cancel() -> cancelled
fail(error) -> failed + sanitized durable error
```

Verify each persisted transition commits before its reliable LiveKit status event, Final remains commit-before-publish, and cancellation/close are idempotent.

**Step 2: Implement Worker lifecycle calls**

Call `runtime.start_replay()` after the target track is subscribed and before ASR startup. Call `runtime.begin_finalizing()` after the audio iterator ends and before `TranscriptionSession.finish()`. Map `asyncio.CancelledError` to `cancelled`; all other pipeline failures map to `failed`. Keep terminal-state guards so cleanup cannot overwrite a completed/failed state.

**Step 3: Expand frontend strict event parsing and display**

Accept only the complete Stage 6 status set. Display restored `error_code` / `error_message` from HTTP and live `session.error` messages, add styles for intermediate/cancelled states, and preserve completed history/no-RTC behavior.

**Step 4: Run focused backend and frontend checks**

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest tests\test_caption_runtime.py tests\test_caption_publisher.py tests\test_worker_entrypoint.py -q -p no:cacheprovider
Set-Location ..\frontend
pnpm typecheck
```

Expected: all focused tests and TypeScript checking pass.

### Task 3: Required timeout configuration and stable error taxonomy

**Files:**
- Create: `backend/app/errors.py`
- Modify: `backend/app/settings.py`
- Modify: `.env.example`
- Modify: `backend/app/replay/decoder.py`
- Modify: `backend/app/replay/source.py`
- Modify: `backend/app/worker/entrypoint.py`
- Modify: `backend/app/captions/runtime.py`
- Modify: `scripts/run_demo_replay.py`
- Modify: `backend/tests/test_settings.py`
- Modify: `backend/tests/test_replay_decoder.py`
- Modify: `backend/tests/test_replay_source.py`
- Modify: `backend/tests/test_logging.py`

**Step 1: Write failing timeout and classification tests**

Add positive settings for:

```text
LIVEKIT_CONNECT_TIMEOUT_SECONDS
FFMPEG_START_TIMEOUT_SECONDS
```

Retain the existing ASR start/finish and DeepSeek request timeouts. Test timeout cleanup and the exact stable categories:

```text
configuration_error
media_decode_error
livekit_error
asr_auth_error
asr_stream_error
persistence_error
deepseek_error
export_error
```

**Step 2: Implement bounded startup/connect operations**

Wrap FFmpeg process creation and Replay/Worker LiveKit connection in finite `asyncio.wait_for` calls. Convert timeouts to sanitized project errors, reap any acquired process/room/source, and ensure logs use stable categories with `session_id`.

**Step 3: Report Replay-side terminal failures**

Add a narrow Session failure API used by `run_demo_replay.py` for `media_decode_error`, `livekit_error`, or cancellation. The server chooses the user-visible message from the allowed category; the client never submits raw exception text. State-machine rules prevent a late Worker completion from overwriting the terminal result.

**Step 4: Run focused tests**

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest tests\test_settings.py tests\test_replay_decoder.py tests\test_replay_source.py tests\test_logging.py tests\test_sessions_api.py -q -p no:cacheprovider
```

Expected: every timeout/error test passes with no resource warnings.

### Task 4: One-command development supervisors

**Files:**
- Create: `scripts/start_dev.ps1`
- Create: `scripts/start_dev.sh`
- Create: `backend/tests/test_dev_launchers.py`

**Step 1: Write launcher contract tests**

Check both scripts provide a non-mutating validation mode, require `.env`, locate the correct Python/pnpm executables, migrate the database before launch, start FastAPI/Worker/Next.js, write logs under `.runtime`, print the separate Replay command, and register exact child cleanup.

**Step 2: Implement PowerShell supervisor**

`scripts/start_dev.ps1 -CheckOnly` validates configuration/dependencies and prints the three commands without starting them. Normal mode upgrades Alembic, starts the three exact children hidden with per-process logs, records PIDs, waits in the foreground, and recursively stops only its recorded trees on Ctrl+C.

**Step 3: Implement POSIX supervisor**

`scripts/start_dev.sh --check-only` mirrors validation. Normal mode runs migration, starts the same three processes in the background, records PIDs, and uses `trap` plus `wait` for cleanup.

**Step 4: Run contract and check-only validation**

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest tests\test_dev_launchers.py -q -p no:cacheprovider
Set-Location ..
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_dev.ps1 -CheckOnly
```

Expected: tests pass; check-only reports API, Worker, Frontend and Replay commands without leaving listeners or child processes.

### Task 5: Credential-free Stage 6 integration test and verifier

**Files:**
- Create: `backend/tests/test_stage6_caption_pipeline.py`
- Create: `scripts/verify_stage6_local.py`

**Step 1: Build one real local pipeline around a Fake ASR Provider**

The test must use:

- `TranscriptionSession` with a `FakeASRProvider`;
- normalized `Partial -> Partial -> Final`;
- the real `TranscriptReconciler` through `WorkerCaptionRuntime`;
- real SQLite persistence;
- the real FastAPI JSON export endpoint;
- no LiveKit, Bailian, or DeepSeek network.

**Step 2: Assert the full local contract**

Require revisions `[1,2,3]`, exactly one durable Final, state sequence `replaying -> transcribing -> finalizing -> completed`, JSON export text/timing, no Draft persistence, clean Queue/tasks/provider/database shutdown, and a second independent run with no cross-session contamination.

**Step 3: Add a CLI verifier and regression test**

`scripts/verify_stage6_local.py` runs the same bounded scenario and emits one JSON line containing `status=ok`, state sequence, Final/export counts, isolation result, and `clean_shutdown=true`.

**Step 4: Run the required compact suite and verifier**

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest `
  tests\test_reconciler.py `
  tests\test_exporters.py `
  tests\test_replay_clock.py `
  tests\test_script_parser.py `
  tests\test_stage6_caption_pipeline.py `
  -q -p no:cacheprovider
Set-Location ..
.\backend\.venv\Scripts\python.exe scripts\verify_stage6_local.py
```

Expected: compact suite passes and verifier reports clean, isolated completion.

### Task 6: Architecture/runbook documentation and final acceptance

**Files:**
- Create: `docs/architecture.md`
- Modify: `docs/api-events.md`
- Modify: `README.md`
- Modify: `docs/stage-records.md`
- Create: `scripts/verify_stage6_e2e.py`

**Step 1: Complete architecture and event documentation**

`docs/architecture.md` records the process graph, audio/caption/data flows, LiveKit responsibility, project responsibility, Bailian adapter boundary, DeepSeek boundary, authoritative Final rule, resource ownership, and future second-chain extension points without implementing them.

`docs/api-events.md` adds the complete Session state sequence, terminal errors, transition rules, and valid JSON examples for Caption, status, progress, metrics, and error events.

**Step 2: Turn README into a clean-environment runbook**

Ensure explicit sections for environment requirements, LiveKit, Bailian, DeepSeek, database initialization, one-command/manual startup, Replay demo, export, validation, shutdown, and common errors. Do not include credentials.

**Step 3: Add the manual E2E orchestrator**

The script reuses the existing API/Replay/Stage 3/Stage 5 verifiers rather than duplicating providers. It accepts an audio file, starts or targets already-running local services, observes the required state sequence, checks source exports, optionally creates/selects one DeepSeek version, and emits a sanitized summary. Cloud calls remain explicit command-line options.

**Step 4: Run all final gates**

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp ..\.runtime\pytest-stage6-final
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current

Set-Location ..
.\backend\.venv\Scripts\python.exe scripts\verify_stage6_local.py

Set-Location frontend
$env:CI='true'
pnpm typecheck
pnpm build
```

Expected: full backend regression, migration, local Stage 6 verifier, frontend typecheck/build, and launcher check-only all pass.

**Step 5: Manual browser/real-service acceptance**

Start with `scripts/start_dev.ps1`, run a fixed Chinese audio Replay, observe live Partial revision and all states through `completed`, verify persisted restoration and four source exports, then generate/select a DeepSeek script. Confirm no browser console errors, no secret leakage, and exact process/resource cleanup. Record exact IDs/results and any environment difference in `docs/stage-records.md`.

