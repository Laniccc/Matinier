from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import get_db_session
from app.artifacts import ArtifactReviewError, ArtifactReviewService
from app.artifacts.exporter import (
    ArtifactExporter,
    ArtifactExportError,
    ArtifactExportFormat,
)
from app.artifacts.models import DerivedArtifact
from app.artifacts.repository import ArtifactRepository
from app.packages.repository import PackageRepository


router = APIRouter(tags=["artifacts"])


class CreateArtifactVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: dict[str, Any]


def _artifact_or_404(
    db_session: Session,
    artifact_id: str,
) -> DerivedArtifact:
    artifact = ArtifactRepository(db_session).get(artifact_id)
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Artifact not found",
        )
    return artifact


@router.get(
    "/api/packages/{package_id}/artifacts",
    response_model=list[DerivedArtifact],
)
def list_artifacts(
    package_id: str,
    db_session: Session = Depends(get_db_session),
) -> list[DerivedArtifact]:
    if PackageRepository(db_session).get(package_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Package not found",
        )
    return ArtifactRepository(db_session).list_for_package(package_id)


@router.get(
    "/api/artifacts/{artifact_id}",
    response_model=DerivedArtifact,
)
def get_artifact(
    artifact_id: str,
    db_session: Session = Depends(get_db_session),
) -> DerivedArtifact:
    return _artifact_or_404(db_session, artifact_id)


@router.post(
    "/api/artifacts/{artifact_id}/versions",
    response_model=DerivedArtifact,
    status_code=status.HTTP_201_CREATED,
)
def create_artifact_version(
    artifact_id: str,
    payload: CreateArtifactVersionRequest,
    db_session: Session = Depends(get_db_session),
) -> DerivedArtifact:
    try:
        artifact = ArtifactReviewService(
            ArtifactRepository(db_session)
        ).create_version(
            artifact_id,
            content=payload.content,
        )
        db_session.commit()
        return _artifact_or_404(db_session, artifact.artifact_id)
    except LookupError:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Artifact not found",
        ) from None
    except ArtifactReviewError as error:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None
    except IntegrityError:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Artifact version conflict",
        ) from None


@router.post(
    "/api/artifacts/{artifact_id}/approve",
    response_model=DerivedArtifact,
)
def approve_artifact(
    artifact_id: str,
    db_session: Session = Depends(get_db_session),
) -> DerivedArtifact:
    try:
        artifact = ArtifactReviewService(
            ArtifactRepository(db_session)
        ).approve(artifact_id)
        db_session.commit()
        return _artifact_or_404(db_session, artifact.artifact_id)
    except LookupError:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Artifact not found",
        ) from None
    except ValueError as error:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None
    except IntegrityError:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Artifact approval conflict",
        ) from None


@router.get(
    "/api/artifacts/{artifact_id}/history",
    response_model=list[DerivedArtifact],
)
def artifact_history(
    artifact_id: str,
    db_session: Session = Depends(get_db_session),
) -> list[DerivedArtifact]:
    try:
        return ArtifactReviewService(
            ArtifactRepository(db_session)
        ).history(artifact_id)
    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Artifact not found",
        ) from None


@router.get(
    "/api/packages/{package_id}/approved-artifacts",
    response_model=list[DerivedArtifact],
)
def list_approved_artifacts(
    package_id: str,
    db_session: Session = Depends(get_db_session),
) -> list[DerivedArtifact]:
    if PackageRepository(db_session).get(package_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Package not found",
        )
    return ArtifactRepository(db_session).list_approved_for_package(package_id)


@router.get("/api/artifacts/{artifact_id}/export")
def export_artifact(
    artifact_id: str,
    export_format: ArtifactExportFormat = Query(alias="format"),
    db_session: Session = Depends(get_db_session),
) -> Response:
    artifact = _artifact_or_404(db_session, artifact_id)
    try:
        result = ArtifactExporter().export(artifact, export_format)
    except ArtifactExportError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None
    return Response(
        content=result.content,
        media_type=f"{result.media_type}; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{result.filename}"'
            ),
            "X-Matinier-Artifact-Kind": artifact.artifact_kind,
            "X-Matinier-Artifact-Version": str(artifact.artifact_version),
            "X-Matinier-Package-Hash": artifact.package_content_hash,
        },
    )
