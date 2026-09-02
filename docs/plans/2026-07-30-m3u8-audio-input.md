# M3U8 Audio Input Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a server-managed public M3U8 audio input that publishes into an existing LiveKit Room and feeds the current caption Worker.

**Architecture:** FastAPI owns an in-process HLS task manager. Each task validates and decodes one public HTTP(S) M3U8 with FFmpeg, joins LiveKit as `hls-{session_id}`, and publishes `caption-input-{session_id}` so the current Worker remains the only ASR and caption persistence path. The browser only starts/stops the task and renders the existing Room/session state.

**Tech Stack:** Python 3.12, FastAPI, asyncio, FFmpeg, LiveKit Python RTC/API, SQLAlchemy/SQLite, pytest, Next.js 16, React 19, TypeScript.

---

## Constraints

- Follow the approved design in `docs/plans/2026-07-30-m3u8-audio-input-design.md`.
- Audio only; do not add HLS video playback or `hls.js`.
- Accept public HTTP/HTTPS sources only. Do not add custom headers, cookies, DRM, or authenticated stream support.
- Keep one active caption input per Room.
- Keep tests lean: one representative test per high-risk behavior, no combinatorial URL matrix, no new frontend test framework.
- The workspace currently has no valid Git metadata. Run the commit steps only if a repository is initialized before execution; otherwise record changed files at each checkpoint.

### Task 1: Build the HLS decoder and LiveKit input task

**Files:**

- Create: `backend/app/hls/__init__.py`
- Create: `backend/app/hls/url_policy.py`
- Create: `backend/app/hls/decoder.py`
- Create: `backend/app/hls/source.py`
- Create: `backend/app/hls/manager.py`
- Modify: `backend/app/settings.py`
- Modify: `.env.example`
- Test: `backend/tests/test_hls_input.py`

**Step 1: Write the representative URL policy test**

Add one parametrized test containing:

- one accepted public HTTPS M3U8;
- one rejected `file://` URL;
- one rejected loopback/private address.

The validator must return a value object with:

```python
@dataclass(frozen=True)
class ValidatedHLSURL:
    fetch_url: str
    display_url: str
```

`display_url` must omit credentials, query, and fragment. Inject hostname resolution into the validator so tests never perform DNS.

**Step 2: Run the URL policy test and confirm it fails**

Run from `backend/`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_hls_input.py -q
```

Expected: failure because `app.hls.url_policy` does not exist.

**Step 3: Implement the minimum URL policy**

In `url_policy.py`:

- accept only `http` and `https`;
- reject missing hosts and embedded username/password;
- resolve all host addresses using an injected async resolver;
- reject loopback, private, link-local, multicast, reserved, unspecified, and non-global addresses;
- return the original full URL only as `fetch_url`;
- build `display_url` without query/fragment/user info and truncate it to 255 characters.

Do not claim this is a production SSRF sandbox; the application remains bound to localhost in the development launcher.

**Step 4: Add one decoder lifecycle test**

Use a fake asyncio subprocess. Verify in one test that the decoder:

- launches FFmpeg without a shell;
- includes the M3U8 URL only in the subprocess argument array;
- outputs 16 kHz mono PCM in 20 ms frames;
- terminates and reaps the subprocess when stopped.

The planned decoder interface is:

```python
class FFmpegHLSDecoder:
    async def frames(self) -> AsyncIterator[PCMFrame]: ...
    async def aclose(self) -> None: ...
```

Reuse `PCMFrame` from `app.replay.decoder`; do not duplicate the frame model.

**Step 5: Implement the decoder**

Use `asyncio.create_subprocess_exec` and Windows `CREATE_NO_WINDOW`. Build an FFmpeg command equivalent to:

```text
ffmpeg -hide_banner -loglevel error
  -rw_timeout <microseconds>
  -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5
  -i <validated-url>
  -vn -ac 1 -ar 16000 -f s16le pipe:1
```

Add a first-frame timeout and a bounded stderr reader. A normal `aclose()` must terminate, wait two seconds, then kill only that exact child if necessary.

Add settings with conservative defaults:

```text
HLS_FIRST_FRAME_TIMEOUT_SECONDS=15
HLS_READ_TIMEOUT_SECONDS=20
HLS_STOP_TIMEOUT_SECONDS=5
```

**Step 6: Add one source/manager contract test**

Use fake LiveKit Room/audio source objects and a fake decoder. In one scenario verify:

- participant identity is `hls-{session_id}`;
- Track is `caption-input-{session_id}`;
- frames reach `capture_frame`;
- `stop(session_id, graceful=True)` closes the decoder and removes the task;
- an injected failure callback receives a decoder exception before the Track is unpublished.

Do not duplicate every ReplaySource test; assert only HLS-specific naming, long-running stop, and failure ordering.

**Step 7: Implement `HLSLiveSource` and `HLSInputManager`**

`HLSLiveSource` owns one LiveKit connection, publishes PCM frames, and guarantees cleanup in `finally`. It accepts callbacks for “published” and “source failed” so lifecycle ordering is testable.

`HLSInputManager` maintains:

```python
self._tasks: dict[str, asyncio.Task[None]]
self._sources: dict[str, HLSLiveSource]
```

Required methods:

```python
async def start(self, request: HLSStartRequest) -> None: ...
async def stop(self, session_id: str, *, graceful: bool) -> bool: ...
async def stop_for_room(self, room_id: str, *, graceful: bool) -> None: ...
async def aclose(self) -> None: ...
async def reconcile_orphans(self) -> int: ...
```

Use LiveKit tokens with publish-only grants. Keep the full URL only inside `HLSStartRequest` and active task memory. On unexpected source failure, write `failed` with `hls_stream_error`; on startup reconciliation, write `hls_process_lost`.

**Step 8: Run the focused test**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_hls_input.py -q
```

Expected: all HLS core tests pass.

**Checkpoint**

If Git is available:

```powershell
git add backend/app/hls backend/app/settings.py backend/tests/test_hls_input.py .env.example
git commit -m "feat: add managed HLS audio source"
```

Otherwise record the Task 1 files before continuing.

### Task 2: Expose HLS start/stop through Room APIs

**Files:**

- Modify: `backend/app/api/rooms.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/persistence/database.py`
- Modify: `backend/tests/conftest.py`
- Modify: `backend/tests/test_rooms_api.py`

**Step 1: Extend the existing Room lifecycle test**

Add a `FakeHLSInputManager` to the existing Room API test module. Extend one lifecycle scenario to verify:

- `POST /api/rooms/{room_id}/hls-inputs` creates a Session with `source_type == "hls"` and sanitized `source_name`;
- the fake manager receives the full URL and Session/Room IDs;
- a second input in the same Room returns 409;
- `POST /api/rooms/{room_id}/hls-inputs/{session_id}/stop` calls graceful stop;
- closing a Room calls `stop_for_room`.

Do not create separate tests for each bullet; keep one readable lifecycle test. Keep one small invalid-public-URL API assertion only if URL validation cannot be proven through the core test.

**Step 2: Run the API test and confirm it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_rooms_api.py -q
```

Expected: 404 for the new endpoint or a missing manager injection point.

**Step 3: Add the API contracts**

In `rooms.py`, add:

```python
class HLSInputCreate(BaseModel):
    url: str = Field(min_length=1, max_length=4096)
    language: str = Field(default="zh-CN", min_length=2, max_length=32)
```

Implement:

```text
POST /api/rooms/{room_id}/hls-inputs
POST /api/rooms/{room_id}/hls-inputs/{session_id}/stop
```

Creation order:

1. require a ready Room;
2. reject an existing nonterminal Session;
3. validate the URL;
4. create/commit an `hls` Session using only `display_url`;
5. start the manager;
6. if task startup itself fails, mark the Session failed and return a safe HTTP error.

The stop endpoint must verify Session ownership and `source_type == "hls"`, request graceful stop, then return the refreshed Session.

**Step 4: Wire application lifecycle**

Allow `create_app()` to accept an injected HLS manager/factory. Store it at `application.state.hls_input_manager`.

In lifespan:

- call `reconcile_orphans()` after the database connection check;
- call `aclose()` before disposing the database.

Expose a small `Database.session()` context manager in `database.py` so background lifecycle writes use the same configured SQLAlchemy session factory without accessing private attributes.

Update `close_room()` to stop active HLS input before deleting the LiveKit Room and applying the existing cancellation semantics.

**Step 5: Run focused API and lifecycle tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_rooms_api.py tests/test_health.py tests/test_api_cold_import.py -q
```

Expected: all selected tests pass and no real network or FFmpeg process is started.

**Checkpoint**

If Git is available:

```powershell
git add backend/app/api/rooms.py backend/app/main.py backend/app/persistence/database.py backend/tests/conftest.py backend/tests/test_rooms_api.py
git commit -m "feat: add Room HLS input API"
```

### Task 3: Make Worker terminal ownership race-safe

**Files:**

- Modify: `backend/app/captions/runtime.py`
- Modify: `backend/app/worker/entrypoint.py`
- Modify: `backend/tests/test_caption_runtime.py`

**Step 1: Add one external-terminal test**

Create one test where the API-side manager changes the Session to `failed` after transcription starts but before Worker finalization. Verify:

- `begin_finalizing()` refreshes the Session and notices the terminal state;
- `complete()` does not overwrite `failed`;
- no `caption_failure_reporting_failed` path is required for this expected race.

Do not add separate tests for completed/cancelled; all terminal states use the same guard.

**Step 2: Run the focused test and confirm it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_caption_runtime.py -q
```

Expected: failure because the runtime attempts `failed -> finalizing`.

**Step 3: Implement terminal refresh**

Before persistent state transitions at finalization boundaries:

- expire/refresh the Session record from SQLite;
- if it is already `completed`, `failed`, or `cancelled`, set the runtime’s `_terminal` flag and skip further state writes/events;
- preserve existing behavior when the Session is nonterminal.

Keep this logic inside `WorkerCaptionRuntime`; do not scatter database status checks through the Worker event handlers.

**Step 4: Run focused Worker tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_caption_runtime.py tests/test_worker_entrypoint.py -q
```

Expected: all selected tests pass.

**Checkpoint**

If Git is available:

```powershell
git add backend/app/captions/runtime.py backend/app/worker/entrypoint.py backend/tests/test_caption_runtime.py
git commit -m "fix: preserve externally failed caption sessions"
```

### Task 4: Add the M3U8 controls and perform one acceptance run

**Files:**

- Modify: `frontend/types/room.ts`
- Modify: `frontend/types/session.ts`
- Modify: `frontend/lib/api.ts`
- Modify: `frontend/components/room-studio.tsx`
- Modify: `frontend/app/globals.css`
- Modify: `README.md`

**Step 1: Extend frontend types and API client**

Add `"hls"` to `CaptionInputType` and Session source types.

Add:

```typescript
export function startHLSInput(
  roomId: string,
  input: { url: string; language: string },
): Promise<CaptionRun>

export function stopHLSInput(
  roomId: string,
  sessionId: string,
): Promise<CaptionRun>
```

Use the new Room endpoints and existing request error extraction.

**Step 2: Implement the minimal RoomStudio UX**

- Add a fourth source selector labelled `M3U8 直播流`.
- Show a URL input only for `hls`.
- Require a nonblank HTTP(S) value before start.
- For HLS, call `startHLSInput` instead of creating a browser `LocalAudioTrack`.
- Track the active HLS Session separately from the browser `ActiveInput`, because the browser owns no media resource.
- Reuse the existing status polling, caption hydration, Room participant display, and error panel.
- On stop, call `stopHLSInput` and poll until terminal.
- Disable start during `acquiring/publishing/stopping` and while any active run exists.
- Do not add video, preview, stream credential fields, or a second status component.

Add only the CSS needed for the URL field and hint; reuse current control classes where possible.

**Step 3: Run frontend static verification**

Run from `frontend/`:

```powershell
pnpm typecheck
pnpm build
```

Expected: both commands exit 0 with no TypeScript or Next.js build error.

**Step 4: Run the complete backend suite once**

Run from `backend/`:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: one clean suite pass. Do not repeat the full suite after every small edit.

**Step 5: Restart the development stack**

Stop the currently recorded development processes with the existing launcher/terminal, then run:

```powershell
.\start_demo.cmd
```

Expected:

- Docker LiveKit, API, Worker, and frontend become ready;
- no stale HLS FFmpeg process exists before starting a stream.

**Step 6: Select and probe one public stream**

At execution time, choose one currently accessible public, unencrypted M3U8 with speech audio. Probe it before using the application:

```powershell
ffprobe -v error -show_entries stream=index,codec_type,codec_name -of json "<public-m3u8-url>"
```

Expected: at least one `"codec_type": "audio"` stream. If the source is offline, replace it once; do not create a large catalog of fallback URLs.

**Step 7: Perform one end-to-end acceptance run**

In `http://127.0.0.1:3000/`:

1. Open the ready Room.
2. Select `M3U8 直播流`, enter the probed URL, choose its spoken language, and start.
3. Confirm an `hls-{session_id}` participant/Track and the Worker appear.
4. Confirm the Session reaches transcription and produces text within 30 seconds, or a clear no-speech result.
5. Stop the input and confirm it reaches a terminal state within five seconds.
6. Refresh and confirm persisted subtitles remain.
7. Confirm no FFmpeg child remains and the Room can start another input.

Inspect the active runtime API/Worker logs once. Success means no unhandled traceback, no leaked FFmpeg process, and no nonterminal orphan Session.

**Step 8: Update operator documentation**

In `README.md`, document:

- public M3U8 audio input workflow;
- FFmpeg requirement;
- supported URL/security limitations;
- normal stop versus cancellation;
- the manual `ffprobe` command.

Do not hard-code the public acceptance URL into product code or automated tests.

**Checkpoint**

If Git is available:

```powershell
git add frontend README.md
git commit -m "feat: add M3U8 input controls"
```

## Final validation

Run exactly one final validation set:

```powershell
Push-Location backend
.\.venv\Scripts\python.exe -m pytest -q
Pop-Location
Push-Location frontend
pnpm typecheck
pnpm build
Pop-Location
```

Expected:

- backend suite passes without unexpected skips/failures;
- frontend typecheck and production build pass;
- the single public-stream acceptance evidence is recorded in the implementation report.

