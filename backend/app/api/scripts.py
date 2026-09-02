from __future__ import annotations

import asyncio
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.dependencies import get_db_session
from app.artifacts.models import DerivedArtifact
from app.artifacts.repository import ArtifactRepository
from app.packages import PackageBuildError, PackageBuilder, PackageRepository
from app.packages.models import TranscriptPackage
from app.persistence.models import SessionRecord
from app.processing.contracts import PackageReader
from app.processing.models import ProcessingJob
from app.processing.repository import ProcessingJobRepository
from app.processing.runner import ProcessingJobRunner
from app.text_processing.markdown import render_processed_script_markdown
from app.text_processing.models import (
    ProcessedScriptContent,
    ScriptSection,
    SourceSegmentSnapshot,
)


router = APIRouter(tags=["scripts"])


class ScriptSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    session_id: str
    provider: str
    model: str
    version: int
    package_id: str
    package_version: int
    package_content_hash: str
    created_at: dt.datetime


class ScriptDetailResponse(ScriptSummaryResponse):
    source_segment_snapshot: list[SourceSegmentSnapshot]
    content: ProcessedScriptContent
    markdown_text: str


def _session_or_404(db_session: Session, session_id: str) -> SessionRecord:
    record = db_session.get(SessionRecord, session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    return record


def _clean_content(artifact: DerivedArtifact) -> ProcessedScriptContent:
    sections = tuple(
        ScriptSection(
            source_segment_ids=tuple(section["source_segment_ids"]),
            start_ms=int(section["start_ms"]),
            end_ms=int(section["end_ms"]),
            clean_text=str(section["clean_text"]),
            notes=tuple(section.get("notes", [])),
        )
        for section in artifact.content["sections"]
    )
    return ProcessedScriptContent(
        title=str(artifact.content["title"]),
        sections=sections,
        warnings=tuple(artifact.content.get("warnings", [])),
    )


def _source_snapshot(package: TranscriptPackage) -> list[SourceSegmentSnapshot]:
    source = PackageReader.effective_source(package)
    snapshot: list[SourceSegmentSnapshot] = []
    for item in source.content.items:
        for segment_id in item.source_segment_ids:
            snapshot.append(
                SourceSegmentSnapshot(
                    segment_id=segment_id,
                    revision=1,
                    language=source.language,
                    raw_text=item.raw_text or item.text,
                    display_text=item.text,
                    audio_start_ms=item.start_ms,
                    audio_end_ms=item.end_ms,
                )
            )
    return snapshot


def _summary(
    artifact: DerivedArtifact,
    package: TranscriptPackage,
) -> ScriptSummaryResponse:
    return ScriptSummaryResponse(
        id=artifact.artifact_id,
        session_id=package.session_id,
        provider=artifact.provider or "unknown",
        model=artifact.model or "unknown",
        version=artifact.artifact_version,
        package_id=artifact.package_id,
        package_version=artifact.package_version,
        package_content_hash=artifact.package_content_hash,
        created_at=artifact.created_at,
    )


def _detail(
    artifact: DerivedArtifact,
    package: TranscriptPackage,
) -> ScriptDetailResponse:
    content = _clean_content(artifact)
    legacy_snapshot = artifact.content.get("legacy_source_segment_snapshot")
    source_snapshot = (
        [SourceSegmentSnapshot.model_validate(item) for item in legacy_snapshot]
        if isinstance(legacy_snapshot, list)
        else _source_snapshot(package)
    )
    legacy_markdown = artifact.content.get("legacy_markdown_text")
    markdown_text = (
        legacy_markdown
        if isinstance(legacy_markdown, str)
        else render_processed_script_markdown(
            content,
            provider=artifact.provider or "unknown",
            model=artifact.model or "unknown",
            version=artifact.artifact_version,
            created_at=artifact.created_at,
        )
    )
    return ScriptDetailResponse(
        **_summary(artifact, package).model_dump(),
        source_segment_snapshot=source_snapshot,
        content=content,
        markdown_text=markdown_text,
    )


def _artifact_or_404(
    db_session: Session,
    artifact_id: str,
) -> tuple[DerivedArtifact, TranscriptPackage]:
    artifact = ArtifactRepository(db_session).get(artifact_id)
    if artifact is None or artifact.artifact_kind != "clean_script":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Script Artifact not found",
        )
    return artifact, PackageRepository(db_session).load(artifact.package_id)


async def _wait_for_job(
    db_session: Session,
    job_id: str,
    *,
    timeout_seconds: float,
) -> ProcessingJob:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        db_session.expire_all()
        job = ProcessingJobRepository(db_session).get(job_id)
        if job is None:
            raise RuntimeError("submitted processing job disappeared")
        if job.status in {"completed", "failed", "cancelled"}:
            return job
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("legacy script request timed out")
        await asyncio.sleep(0.05)


@router.post(
    "/api/sessions/{session_id}/scripts",
    response_model=ScriptDetailResponse,
    status_code=status.HTTP_201_CREATED,
    deprecated=True,
)
async def generate_script(
    session_id: str,
    request: Request,
    db_session: Session = Depends(get_db_session),
) -> ScriptDetailResponse:
    _session_or_404(db_session, session_id)
    try:
        package = PackageBuilder(db_session).build_baseline(session_id)
        db_session.commit()
    except PackageBuildError as error:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Session has no Final captions"
                if "Final captions" in str(error)
                else str(error)
            ),
        ) from None
    except Exception:
        db_session.rollback()
        raise

    runner: ProcessingJobRunner = request.app.state.processing_job_runner
    try:
        job = await runner.submit(
            package_id=package.package_id,
            artifact_kind="clean_script",
            options={},
        )
        timeout = max(
            30.0,
            request.app.state.settings.deepseek_request_timeout_seconds
            * (request.app.state.settings.deepseek_max_retries + 1),
        )
        completed = await _wait_for_job(
            db_session,
            job.job_id,
            timeout_seconds=timeout,
        )
    except TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Script generation is still running; use the Job API",
        ) from None
    if completed.status == "failed":
        code = completed.error_code
        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
                if code == "provider_not_configured"
                else status.HTTP_502_BAD_GATEWAY
            ),
            detail=(
                "DeepSeek is not configured"
                if code == "provider_not_configured"
                else "Script generation failed"
            ),
        )
    if completed.status == "cancelled" or completed.result_artifact_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Script generation was cancelled",
        )
    artifact, artifact_package = _artifact_or_404(
        db_session,
        completed.result_artifact_id,
    )
    return _detail(artifact, artifact_package)


@router.get(
    "/api/sessions/{session_id}/scripts",
    response_model=list[ScriptSummaryResponse],
    deprecated=True,
)
def list_scripts(
    session_id: str,
    db_session: Session = Depends(get_db_session),
) -> list[ScriptSummaryResponse]:
    _session_or_404(db_session, session_id)
    artifacts: list[tuple[DerivedArtifact, TranscriptPackage]] = []
    package_repository = PackageRepository(db_session)
    artifact_repository = ArtifactRepository(db_session)
    for record in package_repository.list_for_session(session_id):
        if record.content_hash is None:
            continue
        package = package_repository.load(record.id)
        artifacts.extend(
            (artifact, package)
            for artifact in artifact_repository.list_for_package(record.id)
            if artifact.artifact_kind == "clean_script"
        )
    artifacts.sort(
        key=lambda item: (item[0].created_at, item[0].artifact_id),
        reverse=True,
    )
    return [_summary(artifact, package) for artifact, package in artifacts]


@router.get(
    "/api/scripts/{script_id}",
    response_model=ScriptDetailResponse,
    deprecated=True,
)
def get_script(
    script_id: str,
    db_session: Session = Depends(get_db_session),
) -> ScriptDetailResponse:
    artifact, package = _artifact_or_404(db_session, script_id)
    return _detail(artifact, package)


@router.get("/api/scripts/{script_id}/export", deprecated=True)
def export_script(
    script_id: str,
    db_session: Session = Depends(get_db_session),
) -> Response:
    artifact, package = _artifact_or_404(db_session, script_id)
    detail = _detail(artifact, package)
    return Response(
        content=detail.markdown_text.encode("utf-8"),
        media_type="text/markdown",
        headers={
            "Content-Disposition": (
                f'attachment; filename="clean-script-v{artifact.artifact_version}.md"'
            )
        },
    )
