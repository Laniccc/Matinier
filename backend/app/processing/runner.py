from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from app.artifacts.models import ArtifactKind
from app.artifacts.repository import ArtifactRepository
from app.packages.repository import PackageRepository
from app.packages.validator import PackageValidationError
from app.persistence.database import Database
from app.processing.contracts import PackageReader, ProcessorRegistry
from app.processing.models import ProcessingJob
from app.processing.repository import ProcessingJobRepository
from app.processing.service import ArtifactService
from app.text_processing.parser import ScriptOutputError
from app.text_processing.provider import (
    DeepSeekConfigurationError,
    DeepSeekError,
)


logger = logging.getLogger(__name__)


class JobNotCancellableError(ValueError):
    pass


def _safe_failure(error: Exception) -> tuple[str, str]:
    if isinstance(error, DeepSeekConfigurationError):
        return "provider_not_configured", str(error)
    if isinstance(error, DeepSeekError):
        return "provider_error", str(error)
    if isinstance(error, ScriptOutputError):
        return "invalid_provider_output", "Structured provider output was invalid"
    if isinstance(error, PackageValidationError):
        return "package_invalid", str(error)
    if isinstance(error, (LookupError, ValueError)):
        return "invalid_processing_request", str(error)
    return "artifact_processing_failed", "Artifact processing failed"


class ProcessingJobRunner:
    """Single-process Package workflow queue owned by the FastAPI lifespan."""

    def __init__(
        self,
        database: Database,
        registry: ProcessorRegistry,
        *,
        concurrency: int = 1,
    ) -> None:
        if concurrency <= 0:
            raise ValueError("processing job concurrency must be positive")
        self._database = database
        self._registry = registry
        self._concurrency = concurrency
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._active: dict[str, asyncio.Task[None]] = {}
        self._user_cancelled: set[str] = set()
        self._stopping = False

    @property
    def started(self) -> bool:
        return bool(self._workers)

    @property
    def registry(self) -> ProcessorRegistry:
        return self._registry

    async def start(self) -> int:
        if self._workers:
            return 0
        with self._database.session() as db_session:
            recovered = ProcessingJobRepository(
                db_session
            ).recover_interrupted()
            db_session.commit()
        self._stopping = False
        self._workers = [
            asyncio.create_task(
                self._worker(),
                name=f"processing-job-worker-{index + 1}",
            )
            for index in range(self._concurrency)
        ]
        logger.info(
            "processing job runner started",
            extra={
                "event": "processing_job_runner_started",
                "concurrency": self._concurrency,
                "recovered_jobs": recovered,
            },
        )
        return recovered

    async def stop(self) -> None:
        if not self._workers:
            return
        self._stopping = True
        workers = tuple(self._workers)
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self._workers.clear()
        self._active.clear()
        self._user_cancelled.clear()
        logger.info(
            "processing job runner stopped",
            extra={"event": "processing_job_runner_stopped"},
        )

    async def submit(
        self,
        *,
        package_id: str,
        artifact_kind: ArtifactKind,
        options: Mapping[str, Any],
        target_artifact_id: str | None = None,
    ) -> ProcessingJob:
        if not self.started or self._stopping:
            raise RuntimeError("processing job runner is not available")
        with self._database.session() as db_session:
            package = PackageReader(PackageRepository(db_session)).read(package_id)
            workflow = self._registry.get(artifact_kind)
            if target_artifact_id is not None:
                target = ArtifactRepository(db_session).get(target_artifact_id)
                if target is None:
                    raise LookupError(
                        f"target artifact not found: {target_artifact_id}"
                    )
                if target.package_id != package.package_id:
                    raise ValueError(
                        "target artifact belongs to another Package"
                    )
            job = ProcessingJobRepository(db_session).create_queued(
                package_id=package.package_id,
                artifact_kind=artifact_kind,
                target_artifact_id=target_artifact_id,
                provider=workflow.provider_name,
                model=workflow.model,
                options=options,
            )
            db_session.commit()
        await self._queue.put(job.job_id)
        return job

    async def cancel(self, job_id: str) -> ProcessingJob | None:
        with self._database.session() as db_session:
            repository = ProcessingJobRepository(db_session)
            current = repository.get(job_id)
            if current is None:
                return None
            if current.status not in {"queued", "running"}:
                raise JobNotCancellableError(
                    f"job is already {current.status}"
                )
            cancelled = repository.mark_cancelled(job_id)
            db_session.commit()
        self._user_cancelled.add(job_id)
        active = self._active.get(job_id)
        if active is not None:
            active.cancel()
        assert cancelled is not None
        return cancelled

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            execution: asyncio.Task[None] | None = None
            try:
                execution = asyncio.create_task(
                    self._execute_job(job_id),
                    name=f"processing-job-{job_id}",
                )
                self._active[job_id] = execution
                await execution
            except asyncio.CancelledError:
                if self._stopping:
                    if execution is not None and not execution.done():
                        execution.cancel()
                        await asyncio.gather(execution, return_exceptions=True)
                    raise
            finally:
                self._active.pop(job_id, None)
                self._user_cancelled.discard(job_id)
                self._queue.task_done()

    async def _execute_job(self, job_id: str) -> None:
        with self._database.session() as db_session:
            running = ProcessingJobRepository(db_session).mark_running(job_id)
            if running is None:
                db_session.rollback()
                return
            db_session.commit()

        try:
            with self._database.session() as db_session:
                repository = ProcessingJobRepository(db_session)
                current = repository.get(job_id)
                if current is None or current.status != "running":
                    return
                service = ArtifactService(
                    package_reader=PackageReader(PackageRepository(db_session)),
                    artifact_repository=ArtifactRepository(db_session),
                    registry=self._registry,
                )
                artifact = await service.generate(
                    package_id=current.package_id,
                    artifact_kind=current.artifact_kind,
                    options=current.options,
                    target_artifact_id=current.target_artifact_id,
                )
                db_session.expire_all()
                refreshed = repository.get(job_id)
                if refreshed is None or refreshed.status != "running":
                    db_session.rollback()
                    return
                completed = repository.mark_completed(
                    job_id,
                    result_artifact_id=artifact.artifact_id,
                )
                if completed is None:
                    db_session.rollback()
                    return
                db_session.commit()
        except asyncio.CancelledError:
            if job_id in self._user_cancelled:
                logger.info(
                    "processing job cancelled",
                    extra={"event": "processing_job_cancelled", "job_id": job_id},
                )
            raise
        except Exception as error:
            error_code, error_message = _safe_failure(error)
            with self._database.session() as db_session:
                repository = ProcessingJobRepository(db_session)
                failed = repository.mark_failed(
                    job_id,
                    error_code=error_code,
                    error_message=error_message,
                )
                db_session.commit()
            if failed is not None:
                logger.error(
                    "processing job failed",
                    extra={
                        "event": "processing_job_failed",
                        "job_id": job_id,
                        "error_code": error_code,
                        "internal_error_type": type(error).__name__,
                    },
                )
