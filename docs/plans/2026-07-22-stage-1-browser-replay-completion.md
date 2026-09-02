# Stage 1 Browser Replay Completion Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let the actual frontend and replay CLI share a Session so the browser can visibly observe the Stage 1 Replay Participant and `replay-audio` Track.

**Architecture:** Add optional existing-Session resolution on both clients without changing backend permissions or removing create-new defaults. Track the browser's LiveKit participant and publication events in React state and render the identities/track names needed by the task-book acceptance check.

**Tech Stack:** Python 3.12, httpx, pytest, Next.js 16, React 19, TypeScript 6, LiveKit Client 2.20.2.

**Constraints:** No upload UI, STT, subtitle revision, DeepSeek, or export. This workspace is not a Git repository, so commit steps are omitted.

---

### Task 1: Test and implement CLI Session reuse

**Files:**
- Create: `backend/tests/test_demo_replay_cli.py`
- Modify: `scripts/run_demo_replay.py`

**Steps:**
1. Write async tests using `httpx.MockTransport` that assert no POST occurs when `session_id` is supplied, the existing Session is fetched, and the existing Session ID is used for the replay token.
2. Write a companion test asserting the old no-Session-ID path still POSTs a new `file` Session.
3. Run `backend/.venv/Scripts/python.exe -m pytest tests/test_demo_replay_cli.py -q`; expect failure before implementation.
4. Add `resolve_session(client, input_path, language, session_id)` and `--session-id`; validate the resolved Session before requesting the replay token.
5. Re-run the targeted tests; expect PASS.

### Task 2: Add frontend create/join controls and observable RTC state

**Files:**
- Modify: `frontend/lib/api.ts`
- Modify: `frontend/components/room-connection.tsx`
- Modify: `frontend/app/globals.css`

**Steps:**
1. Add `getSession(sessionId)` and `source_type: "empty" | "file"` to the API client.
2. Add create/join mode controls, an existing Session ID input, and a source type selector for newly created Sessions.
3. Resolve the Session with GET in join mode or POST in create mode before issuing the browser token.
4. Subscribe to participant/track publish/unpublish/connect/disconnect events, rebuild a stable list from `room.remoteParticipants`, and render participant identity plus track names.
5. Clear all remote state on disconnect/error and disable inputs while connecting.
6. Run `npm.cmd run typecheck -- --incremental false`; expect PASS.

### Task 3: Regression, real-room acceptance, and documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/stage-records.md`
- Modify: `scripts/verify_stage1.py` only if required for shared-Session verification.

**Steps:**
1. Run all backend tests; expect all tests PASS with no lifecycle warnings.
2. Run frontend typecheck and production build; expect PASS.
3. Start pinned LiveKit, migrate a temporary SQLite database, and start API/Worker/Frontend.
4. Create/connect a browser-equivalent viewer first, run the replay path with the same Session ID, and assert `replay-{session_id}`, `replay-audio`, Track unpublication, Worker frame statistics, and no active FFmpeg process.
5. Update README with the exact shared-Session browser/CLI flow and update stage records with measured evidence and remaining environment limitations.
6. Re-run the full regression after documentation/code adjustments.

