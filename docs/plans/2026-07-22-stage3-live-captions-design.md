# Stage 3 Live Captions Design

## Goal

Implement the Stage 3 vertical slice from normalized Bailian `ASREvent` values to deterministic caption reconciliation, reliable LiveKit data messages, durable Final snapshots in SQLite, and a Next.js page that replaces Partial captions in place and restores Final captions after refresh.

## Approved architecture

The Worker writes Final captions directly to SQLite and publishes real-time events through LiveKit reliable data packets. Partial captions are never persisted. FastAPI remains a read-oriented snapshot API and does not become an internal event-ingest service or a hidden LiveKit participant.

```text
Bailian ASREvent
  -> TranscriptReconciler
  -> CaptionEvent (segment_id + revision)
       |-> Partial: LiveKit reliable data only
       `-> Final: idempotent SQLite upsert, then LiveKit reliable data

SQLite Final rows -> GET /api/sessions/{id}/segments -> browser snapshot
LiveKit events     -> strict browser decoder -> caption reducer -> UI
```

This was selected over two alternatives:

- Worker-to-API ingestion would centralize writes but add an internal HTTP contract, authentication, retries, and another failure boundary.
- Having FastAPI join every Room would duplicate RTC lifecycle management and turn the API into a media participant.

## Domain model and reconciliation

`TranscriptReconciler` consumes only normalized `ASREvent`; it never sees Bailian SDK/WebSocket objects and never calls DeepSeek or formats database/SRT output.

An accepted Partial or Final produces a `CaptionEvent` containing:

- `session_id`
- `segment_id`
- `revision`
- `status` (`draft` or `final`)
- `text`
- `audio_start_ms`
- `audio_end_ms`
- `confidence`
- `provider_event_id`
- `received_at_ms`

Rules:

1. The first accepted update for a segment is revision 1; every changed accepted update increments it.
2. Repeated provider event IDs and identical event fingerprints are no-ops.
3. Partial replaces the active draft for its `segment_id`.
4. Final removes the active draft and creates one completed segment.
5. Any Partial after a Final for the same segment is ignored.
6. A repeated or stale Final cannot create a duplicate or lower the revision.
7. Completed segments sort by audio start, audio end, then stable finalization order.
8. Missing provider segment IDs are fixed inside the Bailian adapter using task ID plus an incrementing segment number; text hashing is not used.

## Persistence

Stage 3 creates the Segment table now because the API and Worker are separate processes and the required refresh snapshot must cross that boundary. The schema is intentionally compatible with Stage 4:

- internal UUID `id`
- `session_id` and provider/project `segment_id` with a unique constraint
- `track_id`
- `revision`
- `language`
- `raw_text` and `display_text`
- `audio_start_ms`, `audio_end_ms`, `confidence`
- `status`
- `finalized_at`, `created_at`, `updated_at`

Only Final captions are stored in Stage 3. Repository upsert accepts only a higher revision, so duplicate or stale packets are idempotent. The Worker also updates the existing Session row to `running`, `completed`, or `failed` and fills `started_at`/`ended_at`.

`GET /api/sessions/{session_id}/segments` returns Final rows only, ordered by audio time. A missing Session returns 404; an existing Session without Final captions returns an empty list.

## Live event protocol

All messages use LiveKit `publish_data(..., reliable=True, topic="livecaption.events.v1")`. JSON is UTF-8 and bounded to a small payload size.

Every envelope has:

```json
{
  "schema_version": 1,
  "topic": "caption",
  "type": "caption.upsert",
  "session_id": "uuid",
  "sent_at_ms": 0,
  "payload": {}
}
```

Supported messages are:

- `caption.upsert`: one normalized `CaptionEvent`
- `session.status`: `running`, `completed`, or `failed`
- `session.progress`: current audio time in milliseconds
- `session.metrics`: Final count, first Partial latency, average Final latency, Provider error count
- `session.error`: stable error code and user-visible message without credentials/raw authorization data

The browser accepts only schema version 1, the exact LiveKit topic, the current Session ID, known message types, and valid payload field types. Unknown or malformed events are ignored without crashing.

## Worker lifecycle

`TranscriptionSession` gains an async normalized-event handler. A per-Track caption runtime owns the Reconciler, LiveKit publisher, and SQLite repository:

1. `stream_started` marks and publishes `running`.
2. Partial/Final events pass through the Reconciler.
3. Final is committed before its reliable event is published.
4. Every 25 received frames publishes progress (about 500 ms).
5. Successful `finish()` publishes metrics and `completed` before the Job shuts down.
6. Failures update SQLite and publish `session.error` plus `failed`, then close all existing ASR/RTC resources.

Publishing or persistence failures fail the current caption pipeline explicitly; they are not silently ignored or mislabeled as successful recognition.

## Browser state and UI

The pure caption reducer maintains the taskbook fields:

- `sessionStatus`
- `activeDraftSegments`
- `finalSegments`
- `lastRevisionBySegment`
- `error`
- `metricsSummary`
- current audio time

Snapshot rows hydrate Final captions. Live events then merge by `segment_id + revision`; stale messages cannot overwrite newer state. On reconnect the page fetches a Final snapshot before connecting and refreshes it once after the reliable event handler is active, closing the snapshot/connect race while remaining idempotent.

The page displays source filename, Session status, current audio time, a visually lighter active Partial area, ordered Final rows with start/end times, latency metrics, and user-visible errors. New Final rows scroll into view. A completed status remains visible when the Agent leaves the Room instead of being replaced by a generic disconnect error.

## Testing and acceptance

Backend unit tests cover the four required reconciliation cases plus duplicate fingerprints, missing segment IDs, repository stale revisions, ordered snapshot API output, reliable topic/JSON publishing, session lifecycle updates, and error propagation.

Frontend verification uses strict TypeScript compilation and production build; reducer and decoder logic remain pure and independently testable. Manual/real acceptance uses the existing Chinese speech fixture and requires:

- changing Partial text appears in one place;
- one stable Final row is produced;
- refresh restores the Final row from SQLite;
- status changes to completed;
- malformed/unknown packets do not crash the page;
- errors are visible;
- no ASR, AudioStream, FFmpeg, Worker Job, API, or LiveKit process is left behind after verification.

## Stage boundary

Stage 3 does not implement DeepSeek, translation, complex editing, export formats, or Draft persistence. Stage 4 will extend the durable Segment data into history/timeline/export behavior without changing the real-time event contract.
