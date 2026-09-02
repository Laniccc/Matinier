from __future__ import annotations

from .models import (
    FINAL_CATEGORIES,
    CourseCategory,
    CourseDocument,
    KnowledgeItem,
    RealtimeNote,
)


def build_course_document(
    *,
    language: str,
    realtime_notes: tuple[RealtimeNote, ...],
    knowledge_items: tuple[KnowledgeItem, ...],
    warnings: tuple[str, ...],
) -> CourseDocument:
    categories = tuple(
        CourseCategory(
            name=name,
            items=tuple(item for item in knowledge_items if item.category == name),
        )
        for name in FINAL_CATEGORIES
    )
    return CourseDocument(
        title="课程内容整理",
        language=language,
        realtime_notes=realtime_notes,
        categories=categories,
        warnings=warnings,
    )


def course_document_content(document: CourseDocument) -> dict[str, object]:
    return {
        "title": document.title,
        "language": document.language,
        "parts": [
            {
                "id": "realtime-notes",
                "title": "第一部分：实时知识笔记",
                "items": [
                    {
                        "id": item.id,
                        "note_type": item.note_type,
                        "title": item.title,
                        "body": item.body,
                        "start_ms": item.start_ms,
                        "end_ms": item.end_ms,
                        "coordinate": (
                            f"{format_coordinate(item.start_ms)}–"
                            f"{format_coordinate(item.end_ms)}"
                        ),
                        "source_segment_ids": list(item.source_segment_ids),
                        "evidence_item_ids": list(item.evidence_item_ids),
                        "confidence_status": item.confidence_status,
                    }
                    for item in document.realtime_notes
                ],
            },
            {
                "id": "knowledge",
                "title": "第二部分：课程知识点整理",
                "categories": [
                    {
                        "name": category.name,
                        "items": [
                            {
                                "id": item.id,
                                "topic_path": list(item.topic_path),
                                "title": item.title,
                                "statement": item.statement,
                                "explanation": item.explanation,
                                "evidence_item_ids": list(item.evidence_item_ids),
                                "source_segment_ids": list(item.source_segment_ids),
                                "time_ranges": [
                                    {
                                        "start_ms": start_ms,
                                        "end_ms": end_ms,
                                        "coordinate": (
                                            f"{format_coordinate(start_ms)}–"
                                            f"{format_coordinate(end_ms)}"
                                        ),
                                    }
                                    for start_ms, end_ms in item.time_ranges
                                ],
                                "related_item_ids": list(item.related_item_ids),
                                "confirmation_status": item.confirmation_status,
                            }
                            for item in category.items
                        ],
                    }
                    for category in document.categories
                ],
                "warnings": list(document.warnings),
            },
        ],
    }


def render_course_markdown(document: CourseDocument) -> str:
    lines = ["# 第一部分：实时知识笔记", ""]
    if document.realtime_notes:
        for note in document.realtime_notes:
            coordinate = (
                f"{format_coordinate(note.start_ms)}–{format_coordinate(note.end_ms)}"
            )
            lines.extend(
                [
                    f"## {note.title}",
                    "",
                    f"- 视频坐标：`{coordinate}`",
                    f"- 类型：{note.note_type}",
                    f"- 状态：{note.confidence_status}",
                    "",
                    note.body,
                    "",
                ]
            )
    else:
        lines.extend(["暂无实时知识笔记。", ""])
    lines.extend(["# 第二部分：课程知识点整理", ""])
    for category in document.categories:
        lines.extend([f"## {category.name}", ""])
        if not category.items:
            lines.extend(["暂无。", ""])
            continue
        for item in category.items:
            coordinates = "、".join(
                f"{format_coordinate(start_ms)}–{format_coordinate(end_ms)}"
                for start_ms, end_ms in item.time_ranges
            )
            lines.extend(
                [
                    f"### {item.title}",
                    "",
                    item.statement,
                    "",
                    item.explanation,
                    "",
                    f"- 主题路径：{' / '.join(item.topic_path)}",
                    f"- 视频坐标：`{coordinates}`",
                    f"- 确认状态：{item.confirmation_status}",
                    "",
                ]
            )
    if document.warnings:
        lines.extend(["## 整理提示", ""])
        lines.extend(f"- {item}" for item in document.warnings)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_coordinate(value_ms: int) -> str:
    if value_ms < 0:
        raise ValueError("coordinate must not be negative")
    hours, remainder = divmod(value_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


__all__ = [
    "build_course_document",
    "course_document_content",
    "format_coordinate",
    "render_course_markdown",
]
