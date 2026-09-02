from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.artifacts.models import ArtifactKind
from app.persistence.models import ProcessingJobRecord, utc_now
from app.processing.models import ProcessingJob


class ProcessingJobRepository:
    def __init__(self, db_session: Session) -> None:
        self._db_session = db_session

    def create_queued(
        self,
        *,
        package_id: str,
        artifact_kind: ArtifactKind,
        target_artifact_id: str | None,
        provider: str | None,
        model: str | None,
        options: Mapping[str, Any],
        created_at: dt.datetime | None = None,
    ) -> ProcessingJob:
        record = ProcessingJobRecord(
            id=str(uuid.uuid4()),
            package_id=package_id,
            target_artifact_id=target_artifact_id,
            result_artifact_id=None,
            artifact_kind=artifact_kind,
            status="queued",
            progress=0,
            provider=provider,
            model=model,
            options_json=dict(options),
            error_code=None,
            error_message=None,
            created_at=created_at or utc_now(),
            started_at=None,
            ended_at=None,
        )
        self._db_session.add(record)
        self._db_session.flush()
        return self._to_domain(record)

    def get_record(self, job_id: str) -> ProcessingJobRecord | None:
        return self._db_session.get(ProcessingJobRecord, job_id)

    def get(self, job_id: str) -> ProcessingJob | None:
        record = self.get_record(job_id)
        return self._to_domain(record) if record is not None else None

    def list_for_package(self, package_id: str) -> list[ProcessingJob]:
        records = self._db_session.scalars(
            select(ProcessingJobRecord)
            .where(ProcessingJobRecord.package_id == package_id)
            .order_by(
                ProcessingJobRecord.created_at.desc(),
                ProcessingJobRecord.id.desc(),
            )
        )
        return [self._to_domain(item) for item in records]

    def mark_running(
        self,
        job_id: str,
        *,
        started_at: dt.datetime | None = None,
    ) -> ProcessingJob | None:
        record = self.get_record(job_id)
        if record is None or record.status != "queued":
            return None
        record.status = "running"
        record.progress = 10
        record.started_at = started_at or utc_now()
        record.error_code = None
        record.error_message = None
        self._db_session.flush()
        return self._to_domain(record)

    def mark_completed(
        self,
        job_id: str,
        *,
        result_artifact_id: str,
        ended_at: dt.datetime | None = None,
    ) -> ProcessingJob | None:
        record = self.get_record(job_id)
        if record is None or record.status != "running":
            return None
        record.status = "completed"
        record.progress = 100
        record.result_artifact_id = result_artifact_id
        record.ended_at = ended_at or utc_now()
        self._db_session.flush()
        return self._to_domain(record)

    def mark_failed(
        self,
        job_id: str,
        *,
        error_code: str,
        error_message: str,
        ended_at: dt.datetime | None = None,
    ) -> ProcessingJob | None:
        record = self.get_record(job_id)
        if record is None or record.status not in {"queued", "running"}:
            return None
        record.status = "failed"
        record.error_code = error_code
        record.error_message = error_message[:1000]
        record.ended_at = ended_at or utc_now()
        self._db_session.flush()
        return self._to_domain(record)

    def mark_cancelled(
        self,
        job_id: str,
        *,
        ended_at: dt.datetime | None = None,
    ) -> ProcessingJob | None:
        record = self.get_record(job_id)
        if record is None or record.status not in {"queued", "running"}:
            return None
        record.status = "cancelled"
        record.error_code = None
        record.error_message = None
        record.ended_at = ended_at or utc_now()
        self._db_session.flush()
        return self._to_domain(record)

    def recover_interrupted(self) -> int:
        result = self._db_session.execute(
            update(ProcessingJobRecord)
            .where(ProcessingJobRecord.status.in_(("queued", "running")))
            .values(
                status="failed",
                error_code="api_process_restarted",
                error_message="API process restarted before the job completed",
                ended_at=utc_now(),
            )
        )
        return result.rowcount or 0

    @staticmethod
    def _to_domain(record: ProcessingJobRecord) -> ProcessingJob:
        return ProcessingJob(
            job_id=record.id,
            package_id=record.package_id,
            target_artifact_id=record.target_artifact_id,
            result_artifact_id=record.result_artifact_id,
            artifact_kind=record.artifact_kind,
            status=record.status,
            progress=record.progress,
            provider=record.provider,
            model=record.model,
            options=record.options_json,
            error_code=record.error_code,
            error_message=record.error_message,
            created_at=record.created_at,
            started_at=record.started_at,
            ended_at=record.ended_at,
        )
