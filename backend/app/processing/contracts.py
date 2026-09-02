from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias

from app.artifacts.models import (
    ArtifactEvidence,
    ArtifactKind,
    DerivedArtifact,
)
from app.packages.models import (
    SourceApprovedDocument,
    SourceRawDocument,
    TranscriptItem,
    TranscriptPackage,
)
from app.packages.repository import PackageRepository
from app.packages.validator import PackageValidator


EffectiveSourceDocument: TypeAlias = SourceRawDocument | SourceApprovedDocument


class PackageReader:
    """Read and validate immutable package input for post-processing."""

    def __init__(
        self,
        repository: PackageRepository,
        validator: PackageValidator | None = None,
    ) -> None:
        self._repository = repository
        self._validator = validator or PackageValidator()

    def read(self, package_id: str) -> TranscriptPackage:
        package = self._repository.load(package_id)
        if package.status not in {"frozen", "superseded"}:
            raise ValueError("only a frozen package can be processed")
        self._validator.validate_or_raise(package)
        return package

    @staticmethod
    def effective_source(
        package: TranscriptPackage,
    ) -> EffectiveSourceDocument:
        source = next(
            (
                document
                for document in package.documents
                if document.document_id
                == package.manifest.effective_source_document_id
            ),
            None,
        )
        if not isinstance(source, (SourceRawDocument, SourceApprovedDocument)):
            raise ValueError("package effective source document is unavailable")
        return source


class EvidenceValidationError(ValueError):
    """A workflow result cannot be traced to its Package input."""


class EvidenceValidator:
    def source_items(
        self,
        package: TranscriptPackage,
    ) -> tuple[TranscriptItem, ...]:
        return PackageReader.effective_source(package).content.items

    def derive(
        self,
        package: TranscriptPackage,
        *,
        evidence_key: str,
        source_item_ids: Sequence[str],
    ) -> ArtifactEvidence:
        if not source_item_ids:
            raise EvidenceValidationError("artifact evidence must not be empty")
        if len(set(source_item_ids)) != len(source_item_ids):
            raise EvidenceValidationError(
                "artifact evidence must not repeat source item IDs"
            )
        items_by_id = {
            item.item_id: item for item in self.source_items(package)
        }
        try:
            referenced = tuple(items_by_id[item_id] for item_id in source_item_ids)
        except KeyError as error:
            raise EvidenceValidationError(
                f"artifact references foreign source item: {error.args[0]}"
            ) from error
        source_segment_ids = tuple(
            dict.fromkeys(
                segment_id
                for item in referenced
                for segment_id in item.source_segment_ids
            )
        )
        return ArtifactEvidence(
            evidence_key=evidence_key,
            source_item_ids=tuple(source_item_ids),
            source_segment_ids=source_segment_ids,
            start_ms=min(item.start_ms for item in referenced),
            end_ms=max(item.end_ms for item in referenced),
        )

    def validate(
        self,
        package: TranscriptPackage,
        evidence: Sequence[ArtifactEvidence],
        *,
        require_complete_source: bool,
        allow_empty_evidence: bool = False,
    ) -> None:
        if not evidence:
            if allow_empty_evidence and not require_complete_source:
                return
            raise EvidenceValidationError("artifact evidence must not be empty")
        source_items = self.source_items(package)
        source_ids = {item.item_id for item in source_items}
        referenced_ids = [
            item_id
            for entry in evidence
            for item_id in entry.source_item_ids
        ]
        unknown = set(referenced_ids) - source_ids
        if unknown:
            raise EvidenceValidationError(
                f"artifact references foreign source items: {sorted(unknown)}"
            )
        if require_complete_source:
            counts = Counter(referenced_ids)
            if set(referenced_ids) != source_ids or any(
                count != 1 for count in counts.values()
            ):
                raise EvidenceValidationError(
                    "clean script must reference every source item exactly once"
                )
        for entry in evidence:
            expected = self.derive(
                package,
                evidence_key=entry.evidence_key,
                source_item_ids=entry.source_item_ids,
            )
            if entry != expected:
                raise EvidenceValidationError(
                    f"artifact evidence mapping mismatch: {entry.evidence_key}"
                )


@dataclass(frozen=True, slots=True)
class ArtifactDraft:
    content: Mapping[str, Any]
    evidence: tuple[ArtifactEvidence, ...]
    provider: str | None
    model: str | None
    target_language: str | None = None
    options: Mapping[str, Any] | None = None


class ArtifactWorkflow(Protocol):
    artifact_kind: ArtifactKind
    workflow_version: str
    requires_complete_source: bool
    allows_empty_evidence: bool
    provider_name: str | None
    model: str | None

    async def run(
        self,
        package: TranscriptPackage,
        options: Mapping[str, Any],
        target_artifact: DerivedArtifact | None = None,
    ) -> ArtifactDraft: ...


PackageProcessor = ArtifactWorkflow


class ProcessorRegistry:
    def __init__(self) -> None:
        self._workflows: dict[ArtifactKind, ArtifactWorkflow] = {}

    def register(
        self,
        workflow: ArtifactWorkflow,
        *,
        replace: bool = False,
    ) -> None:
        if workflow.artifact_kind in self._workflows and not replace:
            raise ValueError(
                f"workflow already registered: {workflow.artifact_kind}"
            )
        self._workflows[workflow.artifact_kind] = workflow

    def get(self, artifact_kind: ArtifactKind) -> ArtifactWorkflow:
        try:
            return self._workflows[artifact_kind]
        except KeyError as error:
            raise LookupError(
                f"artifact workflow is not available: {artifact_kind}"
            ) from error
