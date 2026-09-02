from __future__ import annotations

import logging
import datetime as dt
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_db_session
from app.export import (
    ExportDocument,
    export_markdown,
    export_srt,
    export_transcript_json,
    export_vtt,
)
from app.persistence.models import SessionRecord
from app.persistence.segments import SegmentRepository
from app.persistence.translations import TranslationSegmentRepository
from app.timeline.service import normalize_timeline, normalize_translation_timeline


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sessions", tags=["exports"])
ExportFormat = Literal["json", "srt", "vtt", "markdown"]
ExportContent = Literal["source", "translation"]


def _render_export(
    document: ExportDocument,
    export_format: ExportFormat,
) -> tuple[bytes, str, str]:
    prefix = "translation" if document.content == "translation" else "source"
    if export_format == "json":
        return export_transcript_json(document), "application/json", f"{prefix}.json"
    if export_format == "srt":
        return export_srt(document), "application/x-subrip", f"{prefix}.srt"
    if export_format == "vtt":
        return export_vtt(document), "text/vtt", f"{prefix}.vtt"
    return export_markdown(document), "text/markdown", f"{prefix}.md"


@router.get("/{session_id}/export")
def export_session(
    session_id: str,
    export_format: ExportFormat = Query(alias="format"),
    content: ExportContent = Query(default="source"),
    db_session: Session = Depends(get_db_session),
) -> Response:
    session_record = db_session.get(SessionRecord, session_id)
    if session_record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )

    try:
        if content == "translation":
            records = TranslationSegmentRepository(db_session).list_final(
                session_id
            )
            timeline = normalize_translation_timeline(records)
        else:
            records = SegmentRepository(db_session).list_final(session_id)
            timeline = normalize_timeline(records)
        exported_at = dt.datetime.now(dt.UTC)
        document = ExportDocument(
            session=session_record,
            timeline=timeline,
            content=content,
            exported_at=exported_at,
        )
        rendered_content, media_type, filename = _render_export(
            document,
            export_format,
        )
    except Exception as error:
        logger.error(
            "session export failed",
            extra={
                "process_name": "api",
                "session_id": session_id,
                "event": "session_export_failed",
                "error_type": "export_error",
                "export_format": export_format,
                "internal_error_type": type(error).__name__,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Export failed",
        ) from None

    return Response(
        content=rendered_content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-LiveCaption-Session-Status": session_record.status,
            "X-LiveCaption-Exported-At": exported_at.isoformat(),
            "X-LiveCaption-Partial-Export": str(
                document.partial_export
            ).lower(),
            "X-LiveCaption-Content": document.content,
        },
    )
