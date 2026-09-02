from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.artifacts.models import ArtifactEvidence
from app.packages.models import TranscriptItem, TranscriptPackage
from app.processing.contracts import (
    EvidenceValidationError,
    EvidenceValidator,
    PackageReader,
)


@dataclass(frozen=True, slots=True)
class FactualEvidenceContent:
    evidence: ArtifactEvidence
    evidence_item_ids: tuple[str, ...]
    time_ranges: tuple[dict[str, int | str], ...]
    evidence_excerpts: tuple[dict[str, object], ...]


class FactualEvidenceBuilder:
    """Derive stable factual references from a Package effective source."""

    def __init__(
        self,
        package: TranscriptPackage,
        validator: EvidenceValidator | None = None,
    ) -> None:
        self._package = package
        self._validator = validator or EvidenceValidator()
        self._items = PackageReader.effective_source(package).content.items
        self._items_by_id = {item.item_id: item for item in self._items}
        self._order = {
            item.item_id: index for index, item in enumerate(self._items)
        }

    def ordered_item_ids(
        self,
        item_ids: Sequence[str],
    ) -> tuple[str, ...]:
        normalized = tuple(item_ids)
        if not normalized:
            raise EvidenceValidationError("factual evidence must not be empty")
        if len(set(normalized)) != len(normalized):
            raise EvidenceValidationError(
                "factual evidence must not repeat Package item IDs"
            )
        unknown = set(normalized) - self._items_by_id.keys()
        if unknown:
            raise EvidenceValidationError(
                f"factual evidence references foreign Package items: {sorted(unknown)}"
            )
        return tuple(sorted(normalized, key=self._order.__getitem__))

    def build(
        self,
        *,
        evidence_key: str,
        evidence_item_ids: Sequence[str],
    ) -> FactualEvidenceContent:
        ordered_ids = self.ordered_item_ids(evidence_item_ids)
        evidence = self._validator.derive(
            self._package,
            evidence_key=evidence_key,
            source_item_ids=ordered_ids,
        )
        items = tuple(self._items_by_id[item_id] for item_id in ordered_ids)
        return FactualEvidenceContent(
            evidence=evidence,
            evidence_item_ids=ordered_ids,
            time_ranges=tuple(self._time_range(item) for item in items),
            evidence_excerpts=tuple(self._excerpt(item) for item in items),
        )

    @staticmethod
    def _time_range(item: TranscriptItem) -> dict[str, int | str]:
        return {
            "item_id": item.item_id,
            "start_ms": item.start_ms,
            "end_ms": item.end_ms,
        }

    @staticmethod
    def _excerpt(item: TranscriptItem) -> dict[str, object]:
        return {
            "item_id": item.item_id,
            "text": item.text,
            "source_segment_ids": list(item.source_segment_ids),
            "start_ms": item.start_ms,
            "end_ms": item.end_ms,
        }
