from __future__ import annotations

import datetime as dt
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import get_db_session
from app.packages import PackageBuildError, PackageBuilder, PackageRepository
from app.packages.models import TranscriptPackage
from app.persistence.models import SessionRecord
from app.revisions import (
    RevisionError,
    RevisionRepository,
    RevisionService,
    TranscriptRevision,
    TranscriptRevisionContent,
)
from app.revisions.exporter import RevisionExporter


router = APIRouter(tags=["revisions"])


class CreateRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_summary: str | None = None


class CreateRevisionVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: TranscriptRevisionContent
    change_summary: str | None = None


class RevisionSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision_id: str
    session_id: str
    version: int
    parent_revision_id: str | None
    base_package_id: str
    language: str
    content_hash: str
    change_summary: str | None
    status: str
    item_count: int
    created_at: dt.datetime
    approved_at: dt.datetime | None


def _summary(revision: TranscriptRevision) -> RevisionSummaryResponse:
    return RevisionSummaryResponse(
        revision_id=revision.revision_id,
        session_id=revision.session_id,
        version=revision.version,
        parent_revision_id=revision.parent_revision_id,
        base_package_id=revision.base_package_id,
        language=revision.language,
        content_hash=revision.content_hash,
        change_summary=revision.change_summary,
        status=revision.status,
        item_count=len(revision.content.items),
        created_at=revision.created_at,
        approved_at=revision.approved_at,
    )


def _revision_or_404(db_session: Session, revision_id: str) -> TranscriptRevision:
    revision = RevisionRepository(db_session).get(revision_id)
    if revision is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Revision not found",
        )
    return revision


@router.post(
    "/api/packages/{package_id}/revisions",
    response_model=TranscriptRevision,
    status_code=status.HTTP_201_CREATED,
)
def create_revision(
    package_id: str,
    payload: CreateRevisionRequest | None = None,
    db_session: Session = Depends(get_db_session),
) -> TranscriptRevision:
    try:
        revision = RevisionService(db_session).create_from_package(
            package_id,
            change_summary=payload.change_summary if payload is not None else None,
        )
        db_session.commit()
        return _revision_or_404(db_session, revision.revision_id)
    except LookupError:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Package not found",
        ) from None
    except (RevisionError, ValueError) as error:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None
    except IntegrityError:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Revision version conflict",
        ) from None


@router.get(
    "/api/sessions/{session_id}/revisions",
    response_model=list[RevisionSummaryResponse],
)
def list_revisions(
    session_id: str,
    db_session: Session = Depends(get_db_session),
) -> list[RevisionSummaryResponse]:
    if db_session.get(SessionRecord, session_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    return [
        _summary(item)
        for item in RevisionRepository(db_session).list_for_session(session_id)
    ]


@router.get("/api/revisions/{revision_id}", response_model=TranscriptRevision)
def get_revision(
    revision_id: str,
    db_session: Session = Depends(get_db_session),
) -> TranscriptRevision:
    return _revision_or_404(db_session, revision_id)


@router.post(
    "/api/revisions/{revision_id}/versions",
    response_model=TranscriptRevision,
    status_code=status.HTTP_201_CREATED,
)
def create_revision_version(
    revision_id: str,
    payload: CreateRevisionVersionRequest,
    db_session: Session = Depends(get_db_session),
) -> TranscriptRevision:
    try:
        revision = RevisionService(db_session).create_version(
            revision_id,
            content=payload.content,
            change_summary=payload.change_summary,
        )
        db_session.commit()
        return _revision_or_404(db_session, revision.revision_id)
    except LookupError:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Revision not found",
        ) from None
    except RevisionError as error:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None
    except IntegrityError:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Revision version conflict",
        ) from None


@router.post(
    "/api/revisions/{revision_id}/approve",
    response_model=TranscriptRevision,
)
def approve_revision(
    revision_id: str,
    db_session: Session = Depends(get_db_session),
) -> TranscriptRevision:
    try:
        revision = RevisionService(db_session).approve(revision_id)
        db_session.commit()
        return _revision_or_404(db_session, revision.revision_id)
    except LookupError:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Revision not found",
        ) from None
    except ValueError as error:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None


@router.post(
    "/api/revisions/{revision_id}/packages",
    response_model=TranscriptPackage,
    status_code=status.HTTP_201_CREATED,
)
def build_revision_package(
    revision_id: str,
    db_session: Session = Depends(get_db_session),
) -> TranscriptPackage:
    revision = _revision_or_404(db_session, revision_id)
    try:
        package = PackageBuilder(db_session).build_from_revision(revision)
        db_session.commit()
        return PackageRepository(db_session).load(package.package_id)
    except PackageBuildError as error:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None
    except IntegrityError:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Package version conflict",
        ) from None


@router.get("/api/revisions/{revision_id}/export")
def export_revision(
    revision_id: str,
    format: Literal["srt", "vtt", "markdown"],
    db_session: Session = Depends(get_db_session),
) -> Response:
    revision = _revision_or_404(db_session, revision_id)
    package = PackageRepository(db_session).load(revision.base_package_id)
    content = RevisionExporter().export(revision, package, format)
    extension = "md" if format == "markdown" else format
    media_type = {
        "srt": "application/x-subrip",
        "vtt": "text/vtt",
        "markdown": "text/markdown",
    }[format]
    return Response(
        content=content,
        media_type=f"{media_type}; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="revision-v{revision.version}.{extension}"'
            )
        },
    )
