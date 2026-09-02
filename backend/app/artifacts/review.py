from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.artifacts.models import DerivedArtifact
from app.artifacts.repository import ArtifactRepository


class ArtifactReviewError(ValueError):
    pass


_PROVENANCE_FIELDS = {
    "clean_script": (
        "source_item_ids",
        "source_segment_ids",
        "start_ms",
        "end_ms",
    ),
    "refined_translation": (
        "source_item_ids",
        "source_segment_ids",
        "start_ms",
        "end_ms",
        "source_text",
    ),
    "summary": (
        "evidence_item_ids",
        "source_segment_ids",
        "start_ms",
        "end_ms",
        "time_ranges",
        "evidence_excerpts",
    ),
    "chapter_outline": (
        "evidence_item_ids",
        "source_segment_ids",
        "start_ms",
        "end_ms",
        "time_ranges",
        "evidence_excerpts",
    ),
    "timeline_fact_review": (
        "claim_id",
        "target_path",
        "claim_text",
        "evidence_item_ids",
        "source_segment_ids",
        "start_ms",
        "end_ms",
        "time_ranges",
        "evidence_excerpts",
    ),
}

_COLLECTION_FIELDS = {
    "clean_script": "sections",
    "refined_translation": "sections",
    "summary": "key_points",
    "chapter_outline": "chapters",
    "timeline_fact_review": "reviews",
}


class ArtifactReviewService:
    """Create immutable human versions and manage their approval slot."""

    def __init__(self, repository: ArtifactRepository) -> None:
        self._repository = repository

    def create_version(
        self,
        artifact_id: str,
        *,
        content: Mapping[str, Any],
    ) -> DerivedArtifact:
        parent = self._repository.get(artifact_id)
        if parent is None:
            raise LookupError(f"artifact not found: {artifact_id}")
        normalized = dict(content)
        self._validate_human_content(parent, normalized)
        return self._repository.create_human_version(
            parent_artifact_id=artifact_id,
            content=normalized,
        )

    def approve(self, artifact_id: str) -> DerivedArtifact:
        artifact = self._repository.get(artifact_id)
        if artifact is None:
            raise LookupError(f"artifact not found: {artifact_id}")
        return self._repository.approve(artifact)

    def history(self, artifact_id: str) -> list[DerivedArtifact]:
        artifact = self._repository.get(artifact_id)
        if artifact is None:
            raise LookupError(f"artifact not found: {artifact_id}")
        return self._repository.history(artifact)

    def _validate_human_content(
        self,
        parent: DerivedArtifact,
        content: dict[str, Any],
    ) -> None:
        collection_key = _COLLECTION_FIELDS[parent.artifact_kind]
        original_items = self._object_list(
            parent.content.get(collection_key),
            collection_key,
        )
        edited_items = self._object_list(
            content.get(collection_key),
            collection_key,
        )
        if len(original_items) != len(edited_items):
            raise ArtifactReviewError(
                f"Human edit cannot change {collection_key} topology"
            )
        for index, (original, edited) in enumerate(
            zip(original_items, edited_items, strict=True)
        ):
            for field in _PROVENANCE_FIELDS[parent.artifact_kind]:
                if edited.get(field) != original.get(field):
                    raise ArtifactReviewError(
                        f"Human edit cannot change provenance field "
                        f"{collection_key}[{index}].{field}"
                    )

        self._validate_editable_fields(parent.artifact_kind, content, edited_items)
        if parent.artifact_kind == "refined_translation" and (
            content.get("target_language") != parent.target_language
        ):
            raise ArtifactReviewError(
                "Human edit cannot change refined translation language"
            )
        if parent.artifact_kind == "timeline_fact_review" and (
            content.get("target_artifact")
            != parent.content.get("target_artifact")
        ):
            raise ArtifactReviewError(
                "Human edit cannot change fact-review target"
            )

    @staticmethod
    def _object_list(value: object, field: str) -> list[Mapping[str, Any]]:
        if not isinstance(value, list) or not value:
            raise ArtifactReviewError(f"{field} must be a non-empty list")
        if any(not isinstance(item, dict) for item in value):
            raise ArtifactReviewError(f"{field} entries must be objects")
        return value

    @staticmethod
    def _validate_editable_fields(
        kind: str,
        content: dict[str, Any],
        items: Sequence[Mapping[str, Any]],
    ) -> None:
        warnings = content.get("warnings", [])
        if not isinstance(warnings, list) or any(
            not isinstance(item, str) for item in warnings
        ):
            raise ArtifactReviewError("warnings must be a list of strings")
        required_text = {
            "clean_script": ("clean_text",),
            "refined_translation": ("translated_text",),
            "summary": ("text",),
            "chapter_outline": ("title", "summary"),
            "timeline_fact_review": (),
        }[kind]
        for index, item in enumerate(items):
            for field in required_text:
                value = item.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ArtifactReviewError(
                        f"edited item {index}.{field} must be visible text"
                    )
        if kind in {"clean_script", "refined_translation"} and any(
            not isinstance(item.get("notes"), list)
            or any(not isinstance(note, str) for note in item["notes"])
            for item in items
        ):
            raise ArtifactReviewError("section notes must be a list of strings")
        title = content.get("title")
        if kind == "clean_script" and (
            not isinstance(title, str) or not title.strip()
        ):
            raise ArtifactReviewError("title must be visible text")
        brief = content.get("brief")
        if kind == "summary" and (
            not isinstance(brief, str) or not brief.strip()
        ):
            raise ArtifactReviewError("brief must be visible text")
        if kind == "timeline_fact_review":
            allowed = {
                "supported",
                "partially_supported",
                "contradicted",
                "unsupported",
                "ambiguous",
            }
            if any(item.get("status") not in allowed for item in items):
                raise ArtifactReviewError("fact-review status is invalid")
            if any(
                item.get("explanation") is not None
                and (
                    not isinstance(item.get("explanation"), str)
                    or not item["explanation"].strip()
                )
                for item in items
            ):
                raise ArtifactReviewError(
                    "fact-review explanation must be text or null"
                )
