# Stage 1 Browser Replay Completion Design

## Goal

Close the remaining Stage 1 acceptance gap by letting the real Next.js page and the replay CLI join the same existing Session/LiveKit Room, while preserving the current create-new-session defaults.

## Chosen approach

Use an explicit shared Session ID:

1. The browser can either create a Session or enter an existing Session ID.
2. The replay CLI accepts optional `--session-id`; without it, it keeps creating a new `file` Session as before.
3. Both sides request their own least-privilege token for that Session.
4. The browser renders remote participant identities and their published track names, so `replay-{session_id}` and `replay-audio` are directly visible.

For the deterministic manual flow, create a `file` Session in the page, connect the browser, copy its Session ID, then run the CLI with `--session-id`.

## Components and data flow

- `frontend/lib/api.ts` adds `getSession` and allows `source_type` when creating a Session.
- `frontend/components/room-connection.tsx` adds create/join modes, keeps the existing connection state machine, and derives a stable participant/track view from LiveKit room events.
- `scripts/run_demo_replay.py` validates an existing Session through `GET /api/sessions/{id}` or creates a new one, then obtains the existing replay-scoped token and publishes normally.
- The backend API schema and token permissions do not change.

## Error handling

- Blank or unknown existing Session IDs are rejected before connecting.
- The CLI propagates HTTP 404/validation failures with a non-zero exit.
- Disconnect and failed-connect paths clear stale remote participant state.

## Testing and acceptance

- Add Python tests for CLI create-versus-reuse Session resolution.
- Run all backend tests, frontend typecheck, and Next.js production build.
- Run a real LiveKit integration where a browser-equivalent viewer joins an existing Session and the CLI reuses it; verify Replay Participant/Track visibility, cleanup, and no FFmpeg process.
- Keep Stage 1 boundaries: no upload UI, STT, subtitle logic, DeepSeek, or export.

