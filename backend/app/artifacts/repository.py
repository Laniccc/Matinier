from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.artifacts.models import (
    ArtifactCreator,
    ArtifactEvidence,
    ArtifactKind,
    DerivedArtifact,
    artifact_identity_key,
)
from app.packages.models import TranscriptPackage
from app.persistence.models import DerivedArtifactRecord, utc_now


class ArtifactRepository:
    def __init__(self, db_session: Session) -> None:
        self._db_session = db_session

    def next_version(
        self,
        package_id: str,
        identity_key: str,
    ) -> int:
        current = self._db_session.scalar(
            select(func.max(DerivedArtifactRecord.artifact_version)).where(
                DerivedArtifactRecord.package_id == package_id,
                DerivedArtifactRecord.identity_key == identity_key,
            )
        )
        return (current or 0) + 1

    def create_generated(
        self,
        *,
        package: TranscriptPackage,
        artifact_kind: ArtifactKind,
        target_language: str | None,
        provider: str | None,
        model: str | None,
        workflow_version: str,
        options: Mapping[str, Any],
        content: Mapping[str, Any],
        evidence: Sequence[ArtifactEvidence],
        parent_artifact_id: str | None = None,
        created_by: ArtifactCreator = "model",
        created_at: dt.datetime | None = None,
    ) -> DerivedArtifact:
        target_artifact_id = (
            str(options.get("target_artifact_id"))
            if options.get("target_artifact_id") is not None
            else parent_artifact_id
        )
        identity_key = artifact_identity_key(
            artifact_kind,
            target_language=target_language,
            target_artifact_id=target_artifact_id,
        )
        record = DerivedArtifactRecord(
            id=str(uuid.uuid4()),
            package_id=package.package_id,
            package_version=package.package_version,
            package_content_hash=package.content_hash,
            artifact_kind=artifact_kind,
            identity_key=identity_key,
            artifact_version=self.next_version(
                package.package_id,
                identity_key,
            ),
            target_language=target_language,
            status="generated",
            provider=provider,
            model=model,
            workflow_version=workflow_version,
            options_json=dict(options),
            content_json=dict(content),
            evidence_json=[item.model_dump(mode="json") for item in evidence],
            parent_artifact_id=parent_artifact_id,
            created_by=created_by,
            created_at=created_at or utc_now(),
            approved_at=None,
        )
        self._db_session.add(record)
        self._db_session.flush()
        return self._to_domain(record)

    def create_human_version(
        self,
        *,
        parent_artifact_id: str,
        content: Mapping[str, Any],
        created_at: dt.datetime | None = None,
    ) -> DerivedArtifact:
        """Append a human-reviewed version without mutating its parent."""

        parent = self.get(parent_artifact_id)
        if parent is None:
            raise LookupError(f"parent artifact not found: {parent_artifact_id}")
        record = DerivedArtifactRecord(
            id=str(uuid.uuid4()),
            package_id=parent.package_id,
            package_version=parent.package_version,
            package_content_hash=parent.package_content_hash,
            artifact_kind=parent.artifact_kind,
            identity_key=parent.identity_key,
            artifact_version=self.next_version(
                parent.package_id,
                parent.identity_key,
            ),
            target_language=parent.target_language,
            status="reviewed",
            provider=None,
            model=None,
            workflow_version="human-edit-v1",
            options_json={
                **parent.options,
                "human_edit": {
                    "parent_artifact_id": parent.artifact_id,
                    "parent_artifact_version": parent.artifact_version,
                },
            },
            content_json=dict(content),
            evidence_json=[
                item.model_dump(mode="json") for item in parent.evidence
            ],
            parent_artifact_id=parent.artifact_id,
            created_by="human",
            created_at=created_at or utc_now(),
            approved_at=None,
        )
        self._db_session.add(record)
        self._db_session.flush()
        return self._to_domain(record)

    def get(self, artifact_id: str) -> DerivedArtifact | None:
        record = self._db_session.get(DerivedArtifactRecord, artifact_id)
        return self._to_domain(record) if record is not None else None

    def list_for_package(self, package_id: str) -> list[DerivedArtifact]:
        records = self._db_session.scalars(
            select(DerivedArtifactRecord)
            .where(DerivedArtifactRecord.package_id == package_id)
            .order_by(
                DerivedArtifactRecord.artifact_kind,
                DerivedArtifactRecord.target_language,
                DerivedArtifactRecord.artifact_version.desc(),
                DerivedArtifactRecord.id.desc(),
            )
        )
        return [self._to_domain(item) for item in records]

    def history(self, artifact: DerivedArtifact) -> list[DerivedArtifact]:
        records = self._db_session.scalars(
            select(DerivedArtifactRecord)
            .where(
                DerivedArtifactRecord.package_id == artifact.package_id,
                DerivedArtifactRecord.identity_key == artifact.identity_key,
            )
            .order_by(DerivedArtifactRecord.artifact_version.desc())
        )
        return [self._to_domain(item) for item in records]

    def list_approved_for_package(
        self,
        package_id: str,
    ) -> list[DerivedArtifact]:
        records = self._db_session.scalars(
            select(DerivedArtifactRecord)
            .where(
                DerivedArtifactRecord.package_id == package_id,
                DerivedArtifactRecord.status == "approved",
            )
            .order_by(DerivedArtifactRecord.identity_key)
        )
        return [self._to_domain(item) for item in records]

    def approve(self, artifact: DerivedArtifact) -> DerivedArtifact:
        if artifact.status == "superseded":
            raise ValueError("A superseded Artifact cannot be approved")
        if artifact.status == "approved":
            return artifact
        if artifact.status not in {"generated", "reviewed"}:
            raise ValueError(
                f"An Artifact with status {artifact.status} cannot be approved"
            )

        self._db_session.execute(
            update(DerivedArtifactRecord)
            .where(
                DerivedArtifactRecord.package_id == artifact.package_id,
                DerivedArtifactRecord.identity_key == artifact.identity_key,
                DerivedArtifactRecord.status == "approved",
                DerivedArtifactRecord.id != artifact.artifact_id,
            )
            .values(status="superseded")
        )
        self._db_session.flush()
        record = self._db_session.get(
            DerivedArtifactRecord,
            artifact.artifact_id,
        )
        if record is None:
            raise LookupError(f"artifact not found: {artifact.artifact_id}")
        record.status = "approved"
        record.approved_at = utc_now()
        self._db_session.flush()
        return self._to_domain(record)

    @staticmethod
    def _to_domain(record: DerivedArtifactRecord) -> DerivedArtifact:
        return DerivedArtifact(
            artifact_id=record.id,
            package_id=record.package_id,
            package_version=record.package_version,
            package_content_hash=record.package_content_hash,
            artifact_kind=record.artifact_kind,
            identity_key=record.identity_key,
            artifact_version=record.artifact_version,
            target_language=record.target_language,
            status=record.status,
            provider=record.provider,
            model=record.model,
            workflow_version=record.workflow_version,
            options=record.options_json,
            content=record.content_json,
            evidence=tuple(
                ArtifactEvidence.model_validate(item)
                for item in record.evidence_json
            ),
            parent_artifact_id=record.parent_artifact_id,
            created_by=record.created_by,
            created_at=record.created_at,
            approved_at=record.approved_at,
        )
