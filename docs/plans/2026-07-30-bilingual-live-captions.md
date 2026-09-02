# Bilingual Live Captions Implementation Plan

**Goal:** Keep Fun-ASR as the authoritative source-caption pipeline and add an
independent Alibaba Cloud Model Studio LiveTranslate pipeline so one Room can
show and persist source captions plus one selected target language.

**Provider contract:** Follow the official
`qwen3.5-livetranslate-flash-realtime` WebSocket protocol:

- connect to the workspace-specific `/api-ws/v1/realtime` endpoint;
- configure text-only output with `session.update`;
- set the source language in `session.input_audio_transcription.language`;
- set the target language in `session.translation.language`;
- send base64 PCM16 with `input_audio_buffer.append`;
- reconcile `response.text.text` as drafts and `response.text.done` as Final;
- send `session.finish` and wait for `session.finished`.

## Constraints

- Existing Fun-ASR source captions remain authoritative and retain the current
  `caption.upsert` event and `segments` storage.
- Translation failure must never fail or stop source ASR.
- One target language per Session in this implementation.
- Source and target languages must differ when translation is enabled.
- Keep tests lean: provider protocol, route failure isolation, persistence/event
  contract, and one frontend static verification path.
- Do not place DeepSeek in the realtime path.

## Batch 1 — Provider and parallel audio foundation

1. Add source-language normalization and pass `language_hints` to Fun-ASR.
2. Add optional `target_language` and independent translation status fields to
   Session configuration.
3. Implement a provider-neutral translation model/session boundary and the
   Alibaba Cloud LiveTranslate WebSocket adapter.
4. Add bounded dual-route audio fan-out. Translation startup, send, finish, or
   provider failures degrade only the translation route.

Verification:

```powershell
Push-Location backend
.\.venv\Scripts\python.exe -m pytest tests/test_bailian_provider.py tests/test_live_translation_provider.py tests/test_worker_entrypoint.py tests/test_rooms_api.py -q
Pop-Location
```

## Batch 2 — Events, persistence, and recovery

1. Add `translation_segments` with immutable source-caption separation.
2. Persist Final translations with approximate audio ranges and source segment
   associations; realign when additional source Finals arrive.
3. Publish `translation.upsert` and `translation.status` over the reliable
   LiveKit data topic.
4. Add `GET /api/sessions/{session_id}/translations` for refresh recovery.

Verification:

```powershell
Push-Location backend
.\.venv\Scripts\python.exe -m pytest tests/test_translation_runtime.py tests/test_translation_repository.py tests/test_caption_publisher.py tests/test_sessions_api.py -q
Pop-Location
```

## Batch 3 — Room controls and bilingual view

1. Rename the current control to “源语种” and add a target-language selector.
2. Send both fields for browser and HLS caption runs.
3. Decode and store source/translation revisions independently.
4. Render two labelled caption lanes and hydrate both Final snapshots.

Verification:

```powershell
Push-Location frontend
pnpm typecheck
pnpm build
Pop-Location
```

## Final verification

Run the backend suite once, then frontend typecheck/build once. Perform only one
real Chinese-to-English acceptance run when valid Alibaba Cloud credentials and
a speech-bearing input stream are available.
