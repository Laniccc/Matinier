from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.artifacts.models import ArtifactKind, DerivedArtifact
from app.artifacts.repository import ArtifactRepository
from app.processing.contracts import (
    EvidenceValidator,
    PackageReader,
    ProcessorRegistry,
)


class ArtifactService:
    """Run one Package-only workflow and persist only its complete result."""

    def __init__(
        self,
        *,
        package_reader: PackageReader,
        artifact_repository: ArtifactRepository,
        registry: ProcessorRegistry,
        evidence_validator: EvidenceValidator | None = None,
    ) -> None:
        self._package_reader = package_reader
        self._artifact_repository = artifact_repository
        self._registry = registry
        self._evidence_validator = evidence_validator or EvidenceValidator()

    async def generate(
        self,
        *,
        package_id: str,
        artifact_kind: ArtifactKind,
        options: Mapping[str, Any],
        target_artifact_id: str | None = None,
    ) -> DerivedArtifact:
        package = self._package_reader.read(package_id)
        workflow = self._registry.get(artifact_kind)
        target_artifact = self._load_target(target_artifact_id)
        draft = await workflow.run(package, options, target_artifact)
        self._evidence_validator.validate(
            package,
            draft.evidence,
            require_complete_source=workflow.requires_complete_source,
            allow_empty_evidence=workflow.allows_empty_evidence,
        )
        return self._artifact_repository.create_generated(
            package=package,
            artifact_kind=workflow.artifact_kind,
            target_language=draft.target_language,
            provider=draft.provider,
            model=draft.model,
            workflow_version=workflow.workflow_version,
            options=draft.options if draft.options is not None else options,
            content=draft.content,
            evidence=draft.evidence,
            parent_artifact_id=target_artifact_id,
        )

    def _load_target(self, artifact_id: str | None) -> DerivedArtifact | None:
        if artifact_id is None:
            return None
        artifact = self._artifact_repository.get(artifact_id)
        if artifact is None:
            raise LookupError(f"target artifact not found: {artifact_id}")
        return artifact
