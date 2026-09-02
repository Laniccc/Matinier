from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.artifacts.models import ArtifactKind


JobStatus = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
]


class ProcessingJob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str = Field(min_length=1)
    package_id: str = Field(min_length=1)
    target_artifact_id: str | None
    result_artifact_id: str | None
    artifact_kind: ArtifactKind
    status: JobStatus
    progress: int = Field(ge=0, le=100)
    provider: str | None
    model: str | None
    options: dict[str, Any]
    error_code: str | None
    error_message: str | None
    created_at: dt.datetime
    started_at: dt.datetime | None
    ended_at: dt.datetime | None
