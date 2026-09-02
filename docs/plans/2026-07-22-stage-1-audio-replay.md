# Stage 1 Fixed-Audio Replay Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Decode a repeatable WAV/MP3 input to paced 16 kHz mono PCM16, publish it as `replay-audio`, and have the LiveKit Worker consume and report the complete audio stream without leaked subprocesses or tasks.

**Architecture:** A replay package separates timing, FFmpeg decoding, and LiveKit publication so deterministic unit tests do not require a room. The trusted demo CLI creates a `file` Session through FastAPI, requests a replay-scoped publish token, then runs `ReplaySource`; the Worker selectively subscribes only to `replay-{session_id}` / `replay-audio` and delegates frame accounting to a testable statistics object.

**Tech Stack:** Python 3.12, asyncio subprocesses, FFmpeg 7.x, LiveKit RTC/Agents 1.5.11, FastAPI/Pydantic, pytest.

**Constraints:** Do not add STT, subtitle revision, DeepSeek, upload UI, or export. This workspace is not a Git repository, so the usual per-task commit steps are intentionally omitted.

---

### Task 1: Drift-compensating replay clock

**Files:**
- Create: `backend/app/replay/__init__.py`
- Create: `backend/app/replay/clock.py`
- Create: `backend/tests/test_replay_clock.py`

**Steps:**
1. Write a `FakeClock` test that feeds five 20 ms frames and asserts `audio_elapsed == 0.1`, `monotonic_started_at == 0`, final fake time `== 0.1`, and that an injected 30 ms processing delay is compensated rather than accumulated.
2. Run `python -B -m pytest tests/test_replay_clock.py -q -p no:cacheprovider`; expect import failure.
3. Implement a `Clock` protocol, `SystemClock`, and `ReplayClock.wait_for_frame(duration)` / `wait_until_complete()` anchored to `monotonic_started_at + audio_elapsed`.
4. Re-run the targeted test; expect PASS.

### Task 2: Streaming FFmpeg PCM decoder and fixed fixture

**Files:**
- Create: `backend/app/replay/decoder.py`
- Create: `backend/tests/test_replay_decoder.py`
- Generate: `backend/tests/fixtures/demo_audio.wav`

**Steps:**
1. Generate a deterministic 1-second 440 Hz mono WAV fixture with FFmpeg.
2. Write tests for 16 kHz mono PCM16, 20 ms frames (`320` samples / `640` bytes), partial final-frame handling, WAV and transcoded MP3 inputs, invalid suffix rejection, decode error reporting, and `active_process is None` after success/cancellation.
3. Run the decoder tests; expect failure before implementation.
4. Implement `PCMFrame` and `FFmpegPCMDecoder.frames()` using `asyncio.create_subprocess_exec`, `-f s16le -ac 1 -ar 16000`, buffered exact frame splitting, an asynchronously drained stderr pipe, and a `finally` block that terminates then kills on timeout and always awaits the child.
5. Re-run decoder tests; expect PASS and no FFmpeg child left running.

### Task 3: File Sessions and least-privilege replay tokens

**Files:**
- Modify: `backend/app/api/sessions.py`
- Modify: `backend/app/api/livekit_token.py`
- Modify: `backend/tests/test_sessions_api.py`
- Modify: `backend/tests/test_livekit_token.py`

**Steps:**
1. Add failing tests showing `source_type=file` persists and each POST creates a new Session ID.
2. Add failing token tests for `participant_type=replay`: identity must be exactly `replay-{session_id}`, publishing enabled, subscribing/data disabled; browser defaults remain non-publishing.
3. Run the two targeted test modules; expect FAIL.
4. Extend `SessionCreate.source_type` to `Literal["empty", "file"]` and persist it.
5. Extend `TokenRequest.participant_type` to `Literal["browser", "replay"]`; derive replay identity server-side and issue role-specific grants without allowing arbitrary browser publication.
6. Re-run tests; expect PASS.

### Task 4: ReplaySource and CLI publisher

**Files:**
- Create: `backend/app/replay/source.py`
- Create: `scripts/run_demo_replay.py`
- Create: `backend/tests/test_replay_source.py`

**Steps:**
1. Write mocked RTC tests asserting identity/token flow, `replay-audio` publication, per-frame pacing/capture, playout wait, and cleanup order on success and cancellation.
2. Implement `ReplaySource.run()` with `rtc.Room`, `rtc.AudioSource`, `rtc.LocalAudioTrack`, `TrackPublishOptions`, `wait_for_subscription`, and a `finally` block that clears queued audio on cancellation, unpublishes the Track, closes the AudioSource, and disconnects the Room.
3. Implement the CLI shown in the task book. It validates the file, creates a `file` Session, requests a replay token, logs the new Session ID, and invokes `ReplaySource`.
4. Run targeted tests; expect PASS.

### Task 5: Selective Worker subscription and audio statistics

**Files:**
- Create: `backend/app/worker/audio_stats.py`
- Modify: `backend/app/worker/entrypoint.py`
- Create: `backend/tests/test_audio_stats.py`
- Extend: `backend/tests/test_worker_entrypoint.py`

**Steps:**
1. Write tests for frame count, sample rate, channels, samples per channel, summed duration, UTC receive start/end timestamps, and replay Track filtering.
2. Implement an `AudioFrameStats` accumulator.
3. Register participant/track handlers before `ctx.connect(SUBSCRIBE_NONE)`, subscribe only when identity starts with `replay-` and publication name is `replay-audio`, and consume the resulting track through `rtc.AudioStream`.
4. Emit periodic `replay_audio_progress` logs and one `replay_audio_complete` log with all required metrics.
5. On shutdown remove listeners, close each AudioStream through its owning task, cancel/gather remaining tasks, and emit the existing room-disconnected log.
6. Run worker/statistics tests; expect PASS with no pending-task warnings.

### Task 6: Full regression, real-room acceptance, and documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/stage-records.md`
- Optionally extend: `scripts/verify_stage0.py` only if Stage 0 regression visibility needs it.

**Steps:**
1. Run all backend tests, frontend typecheck, and frontend production build.
2. Start pinned LiveKit, migrate SQLite, and start API/Worker/Frontend.
3. Run `python scripts/run_demo_replay.py --file backend/tests/fixtures/demo_audio.wav` twice; assert different Session IDs, identity format, Track name, Worker frame count, approximately one-second duration, and successful completion.
4. During one run terminate the replay CLI; verify Track unpublication, no FFmpeg process, Worker AudioStream completion, and no unclosed-task warning.
5. Confirm the browser-facing page remains HTTP 200 and observes the replay participant through LiveKit participant state; where in-app browser automation is unavailable, record the RTC observer evidence explicitly.
6. Stop all test processes and Compose resources.
7. Document commands, expected logs, measured timing, cleanup evidence, dependency notes, and the explicit no-STT boundary.

