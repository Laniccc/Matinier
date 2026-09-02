from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Protocol

from .document import (
    build_course_document,
    course_document_content,
    render_course_markdown,
)
from .evidence import parse_knowledge_items
from .language import validate_language
from .models import EvidenceItem, KnowledgeItem, RealtimeNote
from .prompts import final_categories_prompt


class FinalizerCapabilityClient(Protocol):
    async def capability(
        self,
        *,
        name: str,
        session_scope: str,
        input_value: dict[str, object],
        idempotency_key: str | None = None,
    ) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class FinalizationResult:
    document_id: str
    document_version: int
    identity_key: str
    content_hash: str
    language: str
    trigger: str
    completeness: str
    package_id: str
    content: dict[str, object]
    markdown: str


class CourseFinalizer:
    def __init__(
        self,
        capabilities: FinalizerCapabilityClient,
        *,
        session_scope: str,
        output_language: str,
        realtime_notes: tuple[RealtimeNote, ...],
        page_limit: int = 100,
        max_reduce_items: int = 500,
        model_batch_size: int = 4,
        model_batch_chars: int = 8_000,
    ) -> None:
        if (page_limit < 1 or page_limit > 100 or max_reduce_items < 1
                or model_batch_size < 1 or model_batch_chars < 1):
            raise ValueError("invalid finalizer bounds")
        self._capabilities = capabilities
        self._scope = session_scope
        self._language = validate_language(output_language)
        self._realtime_notes = realtime_notes
        self._page_limit = page_limit
        self._max_reduce_items = max_reduce_items
        self._model_batch_size = model_batch_size
        self._model_batch_chars = model_batch_chars

    async def run(
        self,
        *,
        job_id: str,
        trigger: str,
        final_sequence: int,
    ) -> FinalizationResult:
        if not job_id or trigger not in {
            "manual",
            "session_completed",
            "session_failed",
            "session_cancelled",
        }:
            raise ValueError("invalid finalization identity or trigger")
        completeness = (
            "interim"
            if trigger == "manual"
            else "complete"
            if trigger == "session_completed"
            else "partial_terminal"
        )
        prepared = await self._capabilities.capability(
            name="delivery.prepare",
            session_scope=self._scope,
            input_value={
                "trigger": trigger,
                "output_language": self._language,
                "final_sequence": final_sequence,
            },
            idempotency_key=f"course-delivery:{job_id}",
        )
        package_id = _required_string(prepared, "package_id")
        package_hash = _required_string(prepared, "content_hash")

        all_evidence: dict[str, EvidenceItem] = {}
        mapped_groups: list[tuple[KnowledgeItem, ...]] = []
        cursor = 0
        while True:
            page = await self._capabilities.capability(
                name="delivery.query",
                session_scope=self._scope,
                input_value={
                    "package_id": package_id,
                    "document_kinds": ["evidence_index"],
                    "language": self._language,
                    "after_item": cursor,
                    "limit": self._page_limit,
                },
            )
            if page.get("package_id") != package_id or page.get("content_hash") != package_hash:
                raise ValueError("delivery page Package identity changed")
            items = page.get("items")
            if not isinstance(items, list):
                raise ValueError("delivery page items are invalid")
            evidence = _page_evidence(items)
            overlap = set(evidence).intersection(all_evidence)
            if overlap:
                raise ValueError("delivery pages contain duplicate evidence IDs")
            all_evidence.update(evidence)
            if evidence:
                mapped_groups.append(await self._map_page(evidence))
            next_cursor = page.get("next_after_item")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, int) or next_cursor <= cursor:
                raise ValueError("delivery cursor did not advance")
            cursor = next_cursor

        if not all_evidence:
            raise ValueError("Frozen Package contains no evidence")
        mapped = _merge_knowledge_groups(mapped_groups)
        if len(mapped) > self._max_reduce_items:
            raise ValueError("course knowledge exceeds the finalizer item limit")
        reduced = await self._reduce(tuple(mapped), all_evidence)
        closed_notes, note_warnings = _close_realtime_notes(
            self._realtime_notes,
            all_evidence,
            language=self._language,
        )
        document = build_course_document(
            language=self._language,
            realtime_notes=closed_notes,
            knowledge_items=reduced,
            warnings=note_warnings,
        )
        content = course_document_content(document)
        markdown = render_course_markdown(document)
        cited_ids = _document_evidence_ids(closed_notes, reduced)
        if not cited_ids:
            raise ValueError("course document contains no Package evidence")
        evidence_refs = [
            {
                "item_id": item_id,
                "source_segment_ids": list(all_evidence[item_id].source_segment_ids),
                "start_ms": all_evidence[item_id].start_ms,
                "end_ms": all_evidence[item_id].end_ms,
            }
            for item_id in cited_ids
        ]
        identity_key = f"course-notes:{self._language}"
        published = await self._capabilities.capability(
            name="document.publish",
            session_scope=self._scope,
            input_value={
                "identity_key": identity_key,
                "schema_name": "matinier.course-notes",
                "schema_version": "1.0",
                "language": self._language,
                "trigger": trigger,
                "completeness": completeness,
                "source_package_id": package_id,
                "content": content,
                "markdown": markdown,
                "evidence_refs": evidence_refs,
            },
            idempotency_key=f"course-document:{job_id}",
        )
        return FinalizationResult(
            document_id=_required_string(published, "document_id"),
            document_version=_required_int(published, "document_version"),
            identity_key=_required_string(published, "identity_key"),
            content_hash=_required_string(published, "content_hash"),
            language=_required_string(published, "language"),
            trigger=_required_string(published, "trigger"),
            completeness=_required_string(published, "completeness"),
            package_id=package_id,
            content=content,
            markdown=markdown,
        )

    async def _map_page(
        self,
        evidence: dict[str, EvidenceItem],
    ) -> tuple[KnowledgeItem, ...]:
        payload_items = [_evidence_payload(item) for item in evidence.values()]
        groups = []
        for batch in self._model_batches(payload_items):
            allowed = {str(item["item_id"]): evidence[str(item["item_id"])] for item in batch}
            groups.append(await self._model_knowledge(
                task="course.final.map",
                repair_task="course.final.map_repair",
                items=batch,
                evidence=allowed,
            ))
        return _merge_knowledge_groups(groups)

    async def _reduce(
        self,
        items: tuple[KnowledgeItem, ...],
        evidence: dict[str, EvidenceItem],
    ) -> tuple[KnowledgeItem, ...]:
        if not items:
            return ()
        groups = []
        for batch in self._model_batches([_knowledge_payload(item) for item in items]):
            batch_ids = {item["id"] for item in batch}
            for item in batch:
                item["related_item_ids"] = [
                    related for related in item["related_item_ids"] if related in batch_ids
                ]
            allowed_ids = {item_id for item in batch for item_id in item["evidence_item_ids"]}
            groups.append(await self._model_knowledge(
                task="course.final.reduce",
                repair_task="course.final.reduce_repair",
                items=batch,
                evidence={item_id: evidence[item_id] for item_id in allowed_ids},
            ))
        return _merge_knowledge_groups(groups)

    def _model_batches(self, items: list[dict[str, object]]) -> list[list[dict[str, object]]]:
        batches: list[list[dict[str, object]]] = []
        batch: list[dict[str, object]] = []
        characters = 0
        for item in items:
            size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            if size > self._model_batch_chars:
                raise ValueError("one course evidence item exceeds the model input limit")
            if batch and (len(batch) >= self._model_batch_size
                          or characters + size > self._model_batch_chars):
                batches.append(batch)
                batch, characters = [], 0
            batch.append(item)
            characters += size
        if batch:
            batches.append(batch)
        return batches

    async def _model_knowledge(
        self,
        *,
        task: str,
        repair_task: str,
        items: list[dict[str, object]],
        evidence: dict[str, EvidenceItem],
    ) -> tuple[KnowledgeItem, ...]:
        response = await self._invoke_model(task, items)
        try:
            return parse_knowledge_items(response.get("output"), allowed_evidence=evidence)
        except ValueError:
            repaired = await self._invoke_model(repair_task, items)
            return parse_knowledge_items(repaired.get("output"), allowed_evidence=evidence)

    async def _invoke_model(
        self,
        task: str,
        items: list[dict[str, object]],
    ) -> dict[str, object]:
        return await self._capabilities.capability(
            name="model.invoke",
            session_scope=self._scope,
            input_value={
                "input_category": "session_transcript",
                "system_prompt": final_categories_prompt(),
                "user_prompt": (
                    "Extract and organize only evidence-supported course knowledge."
                ),
                "input_payload": {
                    "task": task,
                    "output_language": self._language,
                    "items": items,
                },
                "response_format": "json_object",
                "max_output_tokens": 4_096,
                "timeout_seconds": 30,
            },
        )


def _merge_knowledge_groups(groups: list[tuple[KnowledgeItem, ...]]) -> tuple[KnowledgeItem, ...]:
    if len(groups) == 1:
        return groups[0]
    merged = []
    for index, group in enumerate(groups):
        ids = {item.id: "knowledge-" + hashlib.sha256(
            f"{index}:{item.id}".encode("utf-8")
        ).hexdigest()[:24] for item in group}
        merged.extend(replace(item, id=ids[item.id], related_item_ids=tuple(
            ids[related] for related in item.related_item_ids
        )) for item in group)
    return tuple(merged)


def _page_evidence(items: list[object]) -> dict[str, EvidenceItem]:
    output: dict[str, EvidenceItem] = {}
    for raw in items:
        if not isinstance(raw, dict) or raw.get("document_kind") != "evidence_index":
            raise ValueError("delivery page contains a non-evidence item")
        item_id = _required_string(raw, "item_id")
        source_ids = raw.get("source_segment_ids")
        if not isinstance(source_ids, list):
            raise ValueError("Package evidence source IDs are invalid")
        item = EvidenceItem(
            item_id=item_id,
            text=_required_string(raw, "effective_text"),
            source_segment_ids=tuple(str(value) for value in source_ids),
            start_ms=_required_int(raw, "start_ms", allow_zero=True),
            end_ms=_required_int(raw, "end_ms", allow_zero=True),
            confirmation_status="supported",
        )
        if item_id in output:
            raise ValueError("duplicate Package evidence ID")
        output[item_id] = item
    return output


def _close_realtime_notes(
    notes: tuple[RealtimeNote, ...],
    evidence: dict[str, EvidenceItem],
    *,
    language: str,
) -> tuple[tuple[RealtimeNote, ...], tuple[str, ...]]:
    closed: list[RealtimeNote] = []
    skipped = 0
    for note in notes:
        source_ids = set(note.source_segment_ids)
        matching = tuple(
            item
            for item in evidence.values()
            if source_ids.intersection(item.source_segment_ids)
        )
        if not matching:
            skipped += 1
            continue
        closed.append(
            RealtimeNote(
                id=note.id,
                note_type=note.note_type,
                title=note.title,
                body=note.body,
                start_ms=min(item.start_ms for item in matching),
                end_ms=max(item.end_ms for item in matching),
                source_segment_ids=_ordered_source_ids(matching),
                evidence_item_ids=tuple(item.item_id for item in matching),
                confidence_status=note.confidence_status,
                language=language,
                related_note_ids=note.related_note_ids,
            )
        )
    warnings = (
        (f"{skipped} 条实时笔记因无法闭合到最终 Package 证据而未收入文档。",)
        if skipped
        else ()
    )
    return tuple(closed), warnings


def _ordered_source_ids(items: tuple[EvidenceItem, ...]) -> tuple[str, ...]:
    result: list[str] = []
    for item in items:
        for source_id in item.source_segment_ids:
            if source_id not in result:
                result.append(source_id)
    return tuple(result)


def _document_evidence_ids(
    notes: tuple[RealtimeNote, ...],
    knowledge: tuple[KnowledgeItem, ...],
) -> tuple[str, ...]:
    result: list[str] = []
    for item_ids in (
        *(item.evidence_item_ids for item in notes),
        *(item.evidence_item_ids for item in knowledge),
    ):
        for item_id in item_ids:
            if item_id not in result:
                result.append(item_id)
    return tuple(result)


def _evidence_payload(item: EvidenceItem) -> dict[str, object]:
    return {
        "item_id": item.item_id,
        "text": item.text[:4_000],
        "source_segment_ids": list(item.source_segment_ids),
        "start_ms": item.start_ms,
        "end_ms": item.end_ms,
        "confirmation_status": item.confirmation_status,
    }


def _knowledge_payload(item: KnowledgeItem) -> dict[str, object]:
    return {
        "id": item.id,
        "category": item.category,
        "topic_path": list(item.topic_path),
        "title": item.title[:500],
        "statement": item.statement[:2_000],
        "explanation": item.explanation[:2_000],
        "evidence_item_ids": list(item.evidence_item_ids),
        "related_item_ids": list(item.related_item_ids),
        "confirmation_status": item.confirmation_status,
    }


def _required_string(value: dict[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"Host response {key} is invalid")
    return result


def _required_int(
    value: dict[str, object],
    key: str,
    *,
    allow_zero: bool = False,
) -> int:
    result = value.get(key)
    minimum = 0 if allow_zero else 1
    if not isinstance(result, int) or isinstance(result, bool) or result < minimum:
        raise ValueError(f"Host response {key} is invalid")
    return result


__all__ = ["CourseFinalizer", "FinalizationResult"]
