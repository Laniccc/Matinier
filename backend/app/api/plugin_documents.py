from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.dependencies import get_db_session
from app.persistence.models import MediaSessionRecord, PluginDocumentRecord
from app.plugins.document_contracts import PluginDocumentEvidenceRef
from app.plugins.document_exporter import PluginDocumentExporter
from app.plugins.document_repository import PluginDocumentRepository


router = APIRouter(tags=["plugin-documents"])


class PluginDocumentSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    plugin_id: str
    plugin_version: str
    media_session_id: str
    source_package_id: str
    source_package_version: int
    source_package_hash: str
    identity_key: str
    document_version: int
    schema_name: str
    schema_version: str
    language: str
    trigger: str
    completeness: str
    status: str
    content_hash: str
    created_at: dt.datetime


class PluginDocumentDetailResponse(PluginDocumentSummaryResponse):
    content: dict[str, object]
    markdown: str
    evidence_refs: tuple[PluginDocumentEvidenceRef, ...]


def _summary(record: PluginDocumentRecord) -> PluginDocumentSummaryResponse:
    return PluginDocumentSummaryResponse(
        document_id=record.id,
        plugin_id=record.plugin_id,
        plugin_version=record.plugin_version,
        media_session_id=record.media_session_id,
        source_package_id=record.source_package_id,
        source_package_version=record.source_package_version,
        source_package_hash=record.source_package_hash,
        identity_key=record.identity_key,
        document_version=record.document_version,
        schema_name=record.schema_name,
        schema_version=record.schema_version,
        language=record.language,
        trigger=record.trigger,
        completeness=record.completeness,
        status=record.status,
        content_hash=record.content_hash,
        created_at=record.created_at,
    )


def _detail(record: PluginDocumentRecord) -> PluginDocumentDetailResponse:
    summary = _summary(record).model_dump()
    return PluginDocumentDetailResponse(
        **summary,
        content=record.content_json,
        markdown=record.markdown_text,
        evidence_refs=tuple(
            PluginDocumentEvidenceRef.model_validate(item)
            for item in record.evidence_refs_json
        ),
    )


def _document_or_404(
    db_session: Session,
    document_id: str,
) -> PluginDocumentRecord:
    record = PluginDocumentRepository(db_session).get(document_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plugin document not found",
        )
    return record


@router.get(
    "/api/media-sessions/{media_session_id}/plugin-documents",
    response_model=list[PluginDocumentSummaryResponse],
)
def list_plugin_documents(
    media_session_id: str,
    plugin_id: str | None = Query(default=None, max_length=128),
    identity_key: str | None = Query(default=None, max_length=192),
    language: str | None = Query(default=None, max_length=32),
    db_session: Session = Depends(get_db_session),
) -> list[PluginDocumentSummaryResponse]:
    if db_session.get(MediaSessionRecord, media_session_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MediaSession not found",
        )
    records = PluginDocumentRepository(db_session).list_for_session(
        media_session_id,
        plugin_id=plugin_id,
        identity_key=identity_key,
        language=language,
    )
    return [_summary(item) for item in records]


@router.get(
    "/api/plugin-documents/{document_id}",
    response_model=PluginDocumentDetailResponse,
)
def get_plugin_document(
    document_id: str,
    db_session: Session = Depends(get_db_session),
) -> PluginDocumentDetailResponse:
    return _detail(_document_or_404(db_session, document_id))


@router.get("/api/plugin-documents/{document_id}/export")
def export_plugin_document(
    document_id: str,
    export_format: str = Query(alias="format"),
    db_session: Session = Depends(get_db_session),
) -> Response:
    record = _document_or_404(db_session, document_id)
    if export_format not in {"markdown", "json"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported plugin document export format",
        )
    result = PluginDocumentExporter().export(record, export_format)  # type: ignore[arg-type]
    return Response(
        content=result.content,
        media_type=result.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{result.filename}"',
            "X-Matinier-Document-Hash": record.content_hash,
        },
    )


__all__ = ["router"]
