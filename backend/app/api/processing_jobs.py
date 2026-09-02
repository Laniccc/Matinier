from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.dependencies import (
    get_db_session,
    get_processing_job_runner,
)
from app.artifacts.models import ArtifactKind
from app.packages.repository import PackageRepository
from app.packages.validator import PackageValidationError
from app.processing.models import ProcessingJob
from app.processing.repository import ProcessingJobRepository
from app.processing.runner import (
    JobNotCancellableError,
    ProcessingJobRunner,
)


router = APIRouter(tags=["processing-jobs"])


class ProcessingJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: ArtifactKind
    target_artifact_id: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


@router.post(
    "/api/packages/{package_id}/jobs",
    response_model=ProcessingJob,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_processing_job(
    package_id: str,
    body: ProcessingJobCreate,
    runner: ProcessingJobRunner = Depends(get_processing_job_runner),
) -> ProcessingJob:
    try:
        return await runner.submit(
            package_id=package_id,
            artifact_kind=body.artifact_kind,
            target_artifact_id=body.target_artifact_id,
            options=body.options,
        )
    except LookupError as error:
        detail = str(error)
        if detail.startswith("Package not found") or detail.startswith(
            "target artifact not found"
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=detail,
            ) from None
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        ) from None
    except (PackageValidationError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Processing job runner is unavailable",
        ) from None


@router.get(
    "/api/packages/{package_id}/jobs",
    response_model=list[ProcessingJob],
)
def list_processing_jobs(
    package_id: str,
    db_session: Session = Depends(get_db_session),
) -> list[ProcessingJob]:
    if PackageRepository(db_session).get(package_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Package not found",
        )
    return ProcessingJobRepository(db_session).list_for_package(package_id)


@router.get(
    "/api/processing-jobs/{job_id}",
    response_model=ProcessingJob,
)
def get_processing_job(
    job_id: str,
    db_session: Session = Depends(get_db_session),
) -> ProcessingJob:
    job = ProcessingJobRepository(db_session).get(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Processing job not found",
        )
    return job


@router.post(
    "/api/processing-jobs/{job_id}/cancel",
    response_model=ProcessingJob,
)
async def cancel_processing_job(
    job_id: str,
    runner: ProcessingJobRunner = Depends(get_processing_job_runner),
) -> ProcessingJob:
    try:
        job = await runner.cancel(job_id)
    except JobNotCancellableError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Processing job not found",
        )
    return job
