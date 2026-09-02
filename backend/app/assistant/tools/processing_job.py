from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.artifacts.models import ArtifactKind
from app.assistant.package_binding import PackageBindingService
from app.assistant.tools.contracts import (
    ToolExecutionContext,
    ToolResult,
    ToolSpec,
)
from app.assistant.tools.registry import ToolRegistry
from app.persistence.database import Database
from app.persistence.models import ResultPackageRecord
from app.processing.models import JobStatus, ProcessingJob
from app.processing.repository import ProcessingJobRepository
from app.processing.runner import ProcessingJobRunner


PROCESSING_JOB_TOOL_NAME = "processing.generate_artifact"


class FrozenProcessingToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProcessingJobToolInput(FrozenProcessingToolModel):
    artifact_kind: ArtifactKind
    options: dict[str, Any] = Field(default_factory=dict)
    target_artifact_id: str | None = Field(default=None, max_length=36)


class ProcessingJobToolOutput(FrozenProcessingToolModel):
    job_id: str = Field(min_length=1, max_length=36)
    package_id: str = Field(min_length=1, max_length=36)
    status: JobStatus
    artifact_id: str | None = Field(default=None, max_length=36)


class ProcessingJobToolAdapter:
    """Expose the existing Package workflow queue without reimplementing it."""

    def __init__(
        self,
        database: Database,
        runner: ProcessingJobRunner,
    ) -> None:
        self._database = database
        self._runner = runner

    @property
    def provider_name(self) -> str:
        return "matinier_processing"

    async def execute(self, context: ToolExecutionContext) -> ToolResult:
        request = ProcessingJobToolInput.model_validate(context.arguments)
        package_id = self._bound_package_id(context)
        if package_id is None:
            return ToolResult(
                status="failed",
                error_code="execution_package_unbound",
                error_message=(
                    "This Assistant run is not bound to a frozen transcript Package."
                ),
            )
        job = await self._runner.submit(
            package_id=package_id,
            artifact_kind=request.artifact_kind,
            options=request.options,
            target_artifact_id=request.target_artifact_id,
        )
        return self._pending(job)

    async def reconcile(self, context: ToolExecutionContext) -> ToolResult:
        reference = context.external_reference or {}
        job_id = reference.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            return ToolResult(
                status="failed",
                error_code="processing_job_reference_missing",
                error_message="The processing job reference is unavailable.",
            )
        package_id = self._bound_package_id(context)
        if package_id is None:
            return ToolResult(
                status="failed",
                error_code="execution_package_unbound",
                error_message=(
                    "This Assistant run is not bound to a frozen transcript Package."
                ),
            )
        with self._database.session() as db_session:
            job = ProcessingJobRepository(db_session).get(job_id)
        if job is None or job.package_id != package_id:
            return ToolResult(
                status="failed",
                error_code="processing_job_not_found",
                error_message="The bound processing job is unavailable.",
            )
        if job.status in {"queued", "running"}:
            return self._pending(job)
        if job.status == "completed" and job.result_artifact_id is not None:
            output = ProcessingJobToolOutput(
                job_id=job.job_id,
                package_id=job.package_id,
                status=job.status,
                artifact_id=job.result_artifact_id,
            )
            return ToolResult(
                status="succeeded",
                output=output.model_dump(mode="json"),
                external_reference={"job_id": job.job_id},
            )
        return ToolResult(
            status="failed",
            external_reference={"job_id": job.job_id},
            error_code=job.error_code or f"processing_job_{job.status}",
            error_message=job.error_message or "The processing job did not complete.",
        )

    def _bound_package_id(self, context: ToolExecutionContext) -> str | None:
        with self._database.session() as db_session:
            binding = PackageBindingService(db_session).get_for_execution(
                context.execution_id
            )
            if binding is None:
                return None
            package = db_session.get(ResultPackageRecord, binding.package_id)
            if (
                package is None
                or package.session_id != context.session_id
                or package.version != binding.package_version
                or package.content_hash != binding.package_content_hash
                or package.status not in {"frozen", "superseded"}
            ):
                return None
            return binding.package_id

    @staticmethod
    def _pending(job: ProcessingJob) -> ToolResult:
        output = ProcessingJobToolOutput(
            job_id=job.job_id,
            package_id=job.package_id,
            status=job.status,
            artifact_id=None,
        )
        return ToolResult(
            status="pending",
            output=output.model_dump(mode="json"),
            external_reference={"job_id": job.job_id},
        )


def register_processing_job_tool(
    registry: ToolRegistry,
    database: Database,
    runner: ProcessingJobRunner,
    *,
    timeout_seconds: float = 15.0,
) -> None:
    registry.register(
        ToolSpec(
            name=PROCESSING_JOB_TOOL_NAME,
            version="1",
            capability="artifact.generate",
            effect="local_write",
            input_model=ProcessingJobToolInput,
            output_model=ProcessingJobToolOutput,
            timeout_seconds=timeout_seconds,
            supports_idempotency=False,
            supports_reconciliation=True,
        ),
        ProcessingJobToolAdapter(database, runner),
    )


__all__ = [
    "PROCESSING_JOB_TOOL_NAME",
    "ProcessingJobToolAdapter",
    "ProcessingJobToolInput",
    "ProcessingJobToolOutput",
    "register_processing_job_tool",
]
