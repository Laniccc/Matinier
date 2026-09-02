# HLS Realtime Caption Runtime Hardening

> Execution note: this workspace is not an initialized Git worktree, so the implementation runs in place. Each task has a narrow regression check before the final suite.

**Goal:** Correct the defects exposed by the latest public-M3U8 Chinese-to-English run: bursty audio delivery, blank finals, misleading latency metrics, excessive translation draft traffic, weak lifecycle visibility, long-block presentation, and noisy local-development teardown/security warnings.

**Architecture:** Keep provider and persistence contracts stable. Pace decoded HLS frames at the LiveKit publishing boundary, reject content-free events before reconciliation, calculate ASR metrics from the first delivered audio chunk, throttle only translation draft publication (never provider input or finals), and split long final captions only in the presentation layer so exports retain provider truth.

**Tech Stack:** Python 3.12, asyncio, FastAPI/LiveKit Agents, SQLAlchemy/SQLite, pytest, Next.js/TypeScript.

---

## Task 1: Pace HLS audio and finish LiveKit resources cleanly

**Files:**
- Modify: `backend/app/hls/source.py`
- Modify: `backend/tests/test_hls_input.py`

**Implementation:**
1. Inject a `ReplayClock`-compatible pacer into `HLSLiveSource`.
2. Wait for each decoded frame duration before `capture_frame`, using the same absolute-timeline pacing already used by file replay.
3. On a normal decoder end, wait for the pacing timeline and LiveKit playout queue; on failure/stop, clear the queue.
4. Always unpublish, close the audio source, disconnect the room, then yield to the Windows event loop once so native transports can settle.
5. Extend the existing HLS source test to prove pacing happens before capture and graceful playout happens only on success.

**Verify:**
`backend\.venv\Scripts\python.exe -m pytest backend/tests/test_hls_input.py -q`

## Task 2: Reject blank events and repair ASR metrics/privacy defaults

**Files:**
- Modify: `backend/app/captions/reconciler.py`
- Modify: `backend/app/translation/reconciler.py`
- Modify: `backend/app/transcription/models.py`
- Modify: `backend/app/transcription/session.py`
- Modify: `backend/app/settings.py`
- Modify: `backend/app/worker/entrypoint.py`
- Modify: `backend/tests/test_reconciler.py`
- Modify: `backend/tests/test_transcription_session.py`
- Modify: `backend/tests/test_settings.py`

**Implementation:**
1. Ignore whitespace-only partial/final events in both reconcilers without allocating a revision or final row.
2. Start metric timing when the first audio chunk is actually accepted by the provider, not when the session object is constructed.
3. Ignore blank result events in counts; compute final latency as wall-clock elapsed since first audio minus the provider audio end timestamp, clamped at zero.
4. Add `LOG_PROVIDER_PAYLOADS=false`; raw provider payloads are logged only when explicitly enabled.
5. Permit the exact localhost `devkey/secret` LiveKit pair but reject short production LiveKit secrets through settings validation.
6. Add one focused regression test per behavior family.

**Verify:**
`backend\.venv\Scripts\python.exe -m pytest backend/tests/test_reconciler.py backend/tests/test_transcription_session.py backend/tests/test_settings.py -q`

## Task 3: Reduce translation traffic and add structured lifecycle telemetry

**Files:**
- Modify: `backend/app/translation/runtime.py`
- Modify: `backend/app/worker/entrypoint.py`
- Modify: `backend/app/logging.py`
- Modify: `backend/tests/test_translation_runtime.py`
- Modify: `backend/tests/test_logging.py`

**Implementation:**
1. Add a monotonic 250 ms publication interval for translation drafts; always publish finals and lifecycle states immediately.
2. Count provider revisions separately from published drafts/finals so throttling is visible without losing provider activity.
3. Emit structured start, active, finalizing, completed and failed translation logs plus a terminal summary; log lengths and identifiers, not subtitle text.
4. Pass worker room/participant correlation context into the runtime and add translation fields to the JSON formatter.

**Verify:**
`backend\.venv\Scripts\python.exe -m pytest backend/tests/test_translation_runtime.py backend/tests/test_logging.py backend/tests/test_worker_entrypoint.py -q`

## Task 4: Make the workbench resilient and readable

**Files:**
- Modify: `frontend/lib/captions.ts`
- Modify: `frontend/components/room-studio.tsx`

**Implementation:**
1. Drop blank live/snapshot caption and translation payloads client-side so historical polluted rows do not affect counters.
2. Add a deterministic display-only splitter for long final captions, preferring sentence punctuation and then bounded chunks; distribute display timestamps proportionally.
3. Render split rows but keep counters based on meaningful persisted/provider segments.

**Verify:**
`pnpm --dir frontend lint`
`pnpm --dir frontend build`

## Task 5: Integrated verification and live-run inspection

**Files:**
- Modify if needed: `.env.example`, `README.md`
- Modify if needed: `scripts/start_dev.ps1`

**Implementation:**
1. Document the new payload logging switch and the meaning of latency metrics.
2. Run the focused backend suite and frontend production build.
3. Restart the local stack, create one controlled public-HLS bilingual session, and inspect API/worker logs plus SQLite rows for pacing, blank finals, counts, translations, and teardown warnings.

**Verify:**
`backend\.venv\Scripts\python.exe -m pytest backend/tests/test_hls_input.py backend/tests/test_reconciler.py backend/tests/test_transcription_session.py backend/tests/test_translation_runtime.py backend/tests/test_logging.py backend/tests/test_settings.py backend/tests/test_worker_entrypoint.py -q`
`pnpm --dir frontend build`
