# Stage 2 Bailian Real-time ASR Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Connect the Stage 1 LiveKit PCM replay stream to a bounded, testable Bailian real-time ASR provider and emit normalized partial/final events plus metrics without requiring cloud credentials for local tests.

**Architecture:** Add a vendor-neutral transcription package containing normalized models, errors, a provider contract, a raw asynchronous Bailian WebSocket adapter, a PCM chunker, and a session orchestrator. Inject the session into the existing Worker so fake providers and sockets exercise every local path while production uses one provider connection per replay track.

**Tech Stack:** Python 3.12, asyncio, websockets 16, pytest, LiveKit Agents 1.5.11, Pydantic Settings.

---

**Execution note:** This workspace isn't a Git repository, so the commit steps from the standard workflow are intentionally omitted. Every implementation task retains a failing-test/pass-test checkpoint.

### Task 1: Normalized ASR models, errors, and provider contract

**Files:**
- Create: `backend/app/transcription/__init__.py`
- Create: `backend/app/transcription/models.py`
- Create: `backend/app/transcription/errors.py`
- Create: `backend/app/transcription/provider.py`
- Test: `backend/tests/test_asr_models.py`

**Step 1: Write the failing tests**

Test that final events force `is_final=True`, lifecycle events retain all taskbook fields, metrics calculate first-partial and average-final latency, and each project exception has a stable `code`.

**Step 2: Run the tests and verify failure**

Run: `Set-Location backend; .\.venv\Scripts\python.exe -m pytest tests/test_asr_models.py -q`

Expected: FAIL because `app.transcription` does not exist.

**Step 3: Implement the minimal contract**

Create a string enum with `stream_started`, `partial_result`, `final_result`, `stream_completed`, and `stream_error`; an immutable `ASREvent` dataclass with the complete taskbook field set; latency metrics based on a monotonic session start; typed errors; and an abstract async provider with `start`, `send_audio`, `finish`, `events`, and `aclose`.

**Step 4: Run the tests**

Expected: all model/contract tests PASS.

### Task 2: PCM chunking and bounded input behavior

**Files:**
- Create: `backend/app/transcription/chunker.py`
- Test: `backend/tests/test_audio_chunker.py`

**Step 1: Write failing tests**

Cover five 640-byte LiveKit frames becoming one 3200-byte/100 ms chunk, multiple chunks in one feed, tail flush, invalid settings, and rejection of odd-length PCM16 input.

**Step 2: Verify failure**

Run: `Set-Location backend; .\.venv\Scripts\python.exe -m pytest tests/test_audio_chunker.py -q`

Expected: FAIL because `AudioChunker` does not exist.

**Step 3: Implement**

Use a private `bytearray`, emit exact `sample_rate * channels * sample_width * chunk_ms / 1000` byte slices, and return the remaining aligned bytes from `flush()`.

**Step 4: Verify pass**

Expected: all chunker tests PASS with no unbounded state path.

### Task 3: Bailian WebSocket provider

**Files:**
- Create: `backend/app/transcription/bailian.py`
- Test: `backend/tests/test_bailian_provider.py`
- Modify: `backend/pyproject.toml`
- Modify mechanically: `backend/uv.lock`

**Step 1: Write a scripted fake WebSocket**

The fake captures `additional_headers`, sent JSON and bytes, yields `task-started`, partial/final `result-generated`, `task-finished`, and can raise handshake/status, timeout, or `task-failed` errors.

**Step 2: Write and run failing protocol tests**

Assert the Beijing/Singapore endpoint, Bearer header, UUID task id, duplex `run-task`, PCM/16000 parameters, binary audio, `finish-task`, event fields, authentication classification, finite startup/finish timeout, protocol failure, debug raw payload, and idempotent close.

Run: `Set-Location backend; .\.venv\Scripts\python.exe -m pytest tests/test_bailian_provider.py -q`

Expected: FAIL because `BailianSpeechRecognitionProvider` does not exist.

**Step 3: Implement the provider**

Inject a connector compatible with `websockets.asyncio.client.connect`; never log headers; run one receive task; use futures/events to signal `task-started` and `task-finished`; map `sentence_end` to partial/final; publish terminal lifecycle/error events exactly once; and close on every exit.

**Step 4: Declare and lock the direct dependency**

Add `websockets>=15,<17` to `backend/pyproject.toml`, then run `Set-Location backend; uv lock`.

Expected: the root package in `uv.lock` lists `websockets`; no incompatible package changes.

**Step 5: Run provider tests**

Expected: all fake WebSocket tests PASS and no network is accessed.

### Task 4: Transcription session orchestration and metrics

**Files:**
- Create: `backend/app/transcription/session.py`
- Test: `backend/tests/test_transcription_session.py`
- Modify: `backend/app/logging.py`

**Step 1: Write failing tests with fake providers**

Cover normal partial/final flow, 20 ms to 100 ms aggregation, tail flush, queue-full exception without dropping, one startup-only retry using a new provider, no retry after audio, provider error count, cancellation cleanup, and summary metrics.

**Step 2: Verify failure**

Run: `Set-Location backend; .\.venv\Scripts\python.exe -m pytest tests/test_transcription_session.py -q`

Expected: FAIL because `TranscriptionSession` does not exist.

**Step 3: Implement the session**

Create a bounded `asyncio.Queue[bytes | sentinel]`; `send_frame()` calls `put_nowait()` and raises `ASRBackpressureError` on `QueueFull`; one sender serializes provider writes; one event consumer logs normalized events and updates metrics; `finish()` flushes the chunker and awaits both paths; `aclose()` cancels and gathers tasks idempotently.

**Step 4: Extend JSON metric fields**

Add final count, partial/final latency, provider error count, sent chunk count, and sent byte count to the stable formatter metric allow-list.

**Step 5: Verify pass**

Expected: all orchestration tests PASS and `asyncio.all_tasks()` contains no leaked session tasks after cleanup.

### Task 5: Settings and Worker integration

**Files:**
- Modify: `backend/app/settings.py`
- Modify: `backend/app/worker/entrypoint.py`
- Modify: `backend/tests/test_settings.py`
- Modify: `backend/tests/test_worker_entrypoint.py`
- Modify: `.env.example`

**Step 1: Write failing settings tests**

Assert safe defaults for endpoint override, 100 ms chunking, 20-chunk queue, startup/finish timeouts, and one startup retry; invalid non-positive values must fail validation. API Key and Workspace remain optional until a real Provider is constructed.

**Step 2: Write failing Worker tests**

Inject a fake transcription session factory and assert `start()` happens before frames, every frame's `data` is sent, `finish()` happens after iteration, and failure/cancellation still closes both the session and LiveKit stream.

**Step 3: Run focused tests**

Run: `Set-Location backend; .\.venv\Scripts\python.exe -m pytest tests/test_settings.py tests/test_worker_entrypoint.py -q`

Expected: FAIL on missing settings/session integration.

**Step 4: Implement settings and production factory**

Add aliased environment fields and build one `BailianSpeechRecognitionProvider` per replay track. Keep Provider configuration validation lazy so Stage 0/1 API imports and local tests work without cloud credentials.

**Step 5: Integrate Worker lifecycle**

Start the transcription session, pass `bytes(event.frame.data)` for each observed frame, finish on normal track end, close in `finally`, and preserve the existing `AudioFrameStats` return and logging.

**Step 6: Verify pass**

Expected: focused tests PASS; no real WebSocket connection occurs in tests.

### Task 6: Documentation and full local acceptance

**Files:**
- Modify: `README.md`
- Modify: `docs/stage-records.md`
- Create: `scripts/verify_stage2_local.py`
- Test: `backend/tests/test_stage2_local_verifier.py`

**Step 1: Add a credential-free verifier test**

The script feeds the fixed WAV-derived PCM into a fake provider and prints JSON proving chunk count, partial/final mapping, summary metrics, and clean shutdown.

**Step 2: Implement the verifier and documentation**

Document configuration, architecture, expected console output, queue/retry policy, local verifier command, and the exact real-cloud acceptance command marked PENDING until credentials are present.

**Step 3: Run all local acceptance checks**

Run:

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest -q --basetemp .pytest-tmp\stage2
.\.venv\Scripts\python.exe ..\scripts\verify_stage2_local.py
Set-Location ..\frontend
pnpm typecheck
pnpm build
```

Expected: all backend tests PASS; verifier JSON contains `status=ok`, at least one partial and final event, and `clean_shutdown=true`; frontend typecheck/build PASS.

**Step 4: Record the honest boundary**

Mark protocol simulation and regression checks complete. Keep real Bailian Partial/Final, latency, and two-run cleanup acceptance explicitly pending rather than treating fake responses as cloud acceptance.
