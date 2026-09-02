from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import get_db_session
from app.packages import (
    PackageBuildError,
    PackageBuilder,
    PackageRepository,
    PackageValidator,
    PackageZipExporter,
)
from app.packages.models import (
    PackageManifest,
    PackageValidationResult,
    TranscriptPackage,
)
from app.persistence.models import ResultPackageRecord, SessionRecord


router = APIRouter(tags=["packages"])


class PackageSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    package_id: str
    session_id: str
    package_version: int
    schema_name: str
    schema_version: str
    status: str
    content_hash: str | None
    source_revision_id: str | None
    created_at: dt.datetime
    frozen_at: dt.datetime | None
    superseded_at: dt.datetime | None


def _summary(record: ResultPackageRecord) -> PackageSummaryResponse:
    return PackageSummaryResponse(
        package_id=record.id,
        session_id=record.session_id,
        package_version=record.version,
        schema_name=record.schema_name,
        schema_version=record.schema_version,
        status=record.status,
        content_hash=record.content_hash,
        source_revision_id=record.source_revision_id,
        created_at=record.created_at,
        frozen_at=record.frozen_at,
        superseded_at=record.superseded_at,
    )


def _package_or_404(
    db_session: Session,
    package_id: str,
) -> TranscriptPackage:
    try:
        return PackageRepository(db_session).load(package_id)
    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Package not found",
        ) from None
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Package is not frozen",
        ) from None


@router.post(
    "/api/sessions/{session_id}/packages",
    response_model=TranscriptPackage,
    status_code=status.HTTP_201_CREATED,
)
def build_package(
    session_id: str,
    db_session: Session = Depends(get_db_session),
) -> TranscriptPackage:
    try:
        package = PackageBuilder(db_session).build_baseline(session_id)
        db_session.commit()
        return PackageRepository(db_session).load(package.package_id)
    except LookupError:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        ) from None
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
    except Exception:
        db_session.rollback()
        raise


@router.get(
    "/api/sessions/{session_id}/packages",
    response_model=list[PackageSummaryResponse],
)
def list_packages(
    session_id: str,
    db_session: Session = Depends(get_db_session),
) -> list[PackageSummaryResponse]:
    if db_session.get(SessionRecord, session_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    return [
        _summary(item)
        for item in PackageRepository(db_session).list_for_session(session_id)
    ]


@router.get("/api/packages/{package_id}", response_model=TranscriptPackage)
def get_package(
    package_id: str,
    db_session: Session = Depends(get_db_session),
) -> TranscriptPackage:
    return _package_or_404(db_session, package_id)


@router.get(
    "/api/packages/{package_id}/manifest",
    response_model=PackageManifest,
)
def get_package_manifest(
    package_id: str,
    db_session: Session = Depends(get_db_session),
) -> PackageManifest:
    return _package_or_404(db_session, package_id).manifest


@router.get("/api/packages/{package_id}/export")
def export_package(
    package_id: str,
    db_session: Session = Depends(get_db_session),
) -> Response:
    package = _package_or_404(db_session, package_id)
    content = PackageZipExporter().export(package)
    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="package-v{package.package_version}.zip"'
            ),
            "X-Matinier-Package-Hash": package.content_hash,
        },
    )


@router.post(
    "/api/packages/{package_id}/validate",
    response_model=PackageValidationResult,
)
def validate_package(
    package_id: str,
    db_session: Session = Depends(get_db_session),
) -> PackageValidationResult:
    return PackageValidator().validate(_package_or_404(db_session, package_id))
