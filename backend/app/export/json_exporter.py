from __future__ import annotations

import json

from app.export.models import ExportDocument


def _datetime(value) -> str | None:
    return value.isoformat() if value is not None else None


def export_transcript_json(document: ExportDocument) -> bytes:
    session = document.session
    timeline = document.timeline
    payload = {
        "schema_version": 1,
        "content": document.content,
        "session": {
            "id": session.id,
            "room_name": session.room_name,
            "status": session.status,
            "source": {
                "type": session.source_type,
                "name": session.source_name,
                "language": session.language,
            },
            "started_at": _datetime(session.started_at),
            "ended_at": _datetime(session.ended_at),
            "created_at": _datetime(session.created_at),
        },
        "transcription": {
            "provider": session.asr_provider,
            "model": session.asr_model,
            "metrics": {
                "final_result_count": session.final_result_count,
                "first_partial_latency_ms": session.first_partial_latency_ms,
                "average_final_latency_ms": session.average_final_latency_ms,
                "provider_error_count": session.provider_error_count,
                "sent_audio_chunk_count": session.sent_audio_chunk_count,
                "sent_audio_bytes": session.sent_audio_bytes,
            },
        },
        "timeline": {
            "duration_ms": timeline.duration_ms,
            "segment_count": len(timeline.segments),
        },
        "segments": [
            {
                "id": segment.id,
                "segment_id": segment.segment_id,
                "track_id": segment.track_id,
                "revision": segment.revision,
                "language": segment.language,
                "raw_text": segment.raw_text,
                "display_text": segment.display_text,
                "audio_start_ms": segment.audio_start_ms,
                "audio_end_ms": segment.audio_end_ms,
                "confidence": segment.confidence,
                "status": "final",
                "received_at_ms": segment.received_at_ms,
                "finalized_at": _datetime(segment.finalized_at),
                "created_at": _datetime(segment.created_at),
                "updated_at": _datetime(segment.updated_at),
            }
            for segment in timeline.segments
        ],
    }
    if document.partial_export:
        payload.update(
            {
                "session_status": session.status,
                "exported_at": _datetime(document.exported_at),
                "partial_export": True,
            }
        )
    text = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )
    return f"{text}\n".encode("utf-8")
