# Room Stream Console Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a single-user debug console that manages reusable LiveKit Rooms, publishes browser audio inputs, and shows live captions, participants, tracks, and run history.

**Architecture:** Add persistent managed Rooms above the existing Session model, treating each Session as one CaptionRun. Browser inputs publish named LiveKit audio tracks; the Worker resolves the run from the track name and remains alive for later runs in the same Room.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, SQLite, LiveKit Python API/Agents, Next.js, React, TypeScript, livekit-client.

---

### Task 1: Add managed Room persistence

**Files:**
- Modify: `backend/app/persistence/models.py`
- Create: `backend/app/persistence/rooms.py`
- Create: `backend/alembic/versions/20260728_0006_create_managed_rooms.py`
- Test: `backend/tests/test_room_repository.py`

**Steps:**

1. Write repository tests for creating, listing, renaming and closing Rooms and listing active/newest CaptionRuns.
2. Run `backend\.venv\Scripts\python.exe -m pytest backend/tests/test_room_repository.py -q`; expect failures for missing Room types.
3. Add `RoomRecord`, nullable `SessionRecord.room_id`, repository queries and the migration. Remove the `sessions.room_name` unique constraint with Alembic SQLite batch mode.
4. Run the repository tests; expect all to pass.

### Task 2: Add Room management API

**Files:**
- Create: `backend/app/rooms/livekit_admin.py`
- Create: `backend/app/api/rooms.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_rooms_api.py`

**Steps:**

1. Write API tests for Room CRUD, reusable CaptionRuns, one-active-run conflict, operator token grants, cancellation, close and participant removal.
2. Run the focused tests; expect 404/missing route failures.
3. Implement the API and an injectable LiveKit admin service using `LiveKitAPI.room.delete_room` and `remove_participant`.
4. Include the router and allow `PATCH` through CORS.
5. Run `backend\.venv\Scripts\python.exe -m pytest backend/tests/test_rooms_api.py backend/tests/test_livekit_token.py -q`; expect all to pass.

### Task 3: Generalize Worker input routing

**Files:**
- Modify: `backend/app/worker/audio_stats.py`
- Modify: `backend/app/worker/entrypoint.py`
- Modify: `backend/tests/test_worker_audio.py`
- Modify: `backend/tests/test_worker_entrypoint.py`

**Steps:**

1. Add failing tests for `caption-input-{uuid}` track resolution and legacy replay compatibility.
2. Run focused Worker tests and confirm the new cases fail.
3. Add a target-track resolver, pass an explicit Session ID into audio consumption, stop tasks when tracks are unpublished, and keep the Worker alive after managed input completion.
4. Run all Worker tests; expect all to pass.

### Task 4: Build the Room debug console

**Files:**
- Create: `frontend/types/room.ts`
- Modify: `frontend/types/session.ts`
- Modify: `frontend/lib/api.ts`
- Create: `frontend/components/room-studio.tsx`
- Modify: `frontend/app/page.tsx`
- Modify: `frontend/app/globals.css`

**Steps:**

1. Add typed Room and CaptionRun client functions.
2. Build Room selection/creation/close and automatic operator connection.
3. Add microphone capture, screen/tab audio capture, and local-file Web Audio publication using track name `caption-input-{session_id}`.
4. Add input stop/cleanup and task cancellation on failed publication.
5. Render live caption state, final timeline, participants/tracks, run history, remove-participant actions and exports.
6. Run `pnpm --dir frontend typecheck`; expect no TypeScript errors.
7. Run `pnpm --dir frontend build`; expect a successful production build.

### Task 5: Regression and live smoke test

**Files:**
- Modify: `README.md`
- Modify: `docs/stage-records.md`

**Steps:**

1. Run `backend\.venv\Scripts\python.exe -m pytest backend/tests -q`; expect the existing suite plus new tests to pass.
2. Run `backend\.venv\Scripts\alembic.exe -c backend/alembic.ini upgrade head`; expect revision `20260728_0006`.
3. Restart the one-click demo so API, Worker and frontend load the new code.
4. Use the browser to create a Room, connect, inspect participant/track state and exercise at least one browser input path.
5. Document the debug workflow and clearly note that RTMP/HLS/SRT/OBS Ingress remains a later adapter batch.
