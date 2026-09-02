from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from .session import CourseSessionState


COURSE_COMMANDS = frozenset(
    {"generate_final", "retry_final", "select_version", "set_language"}
)


def build_course_view(state: "CourseSessionState") -> dict[str, object]:
    notes = state.notes[-100:]
    timeline = [
        {
            "id": f"timeline-{index}-{note.id}"[:128],
            "time_ms": note.start_ms,
            "title": f"[{note.note_type}] {note.title}"[:500],
            "body": f"状态：{note.confidence_status} · {note.body}"[:300],
        }
        for index, note in enumerate(notes)
    ]
    realtime_children: list[dict[str, object]] = [
        {
            "id": "realtime-status",
            "type": "badge",
            "text": _status_label(state.status),
            "tone": _status_tone(state.status),
        }
    ]
    if timeline:
        realtime_children.append(
            {"id": "realtime-timeline", "type": "timeline", "items": timeline}
        )
    else:
        realtime_children.append(
            {
                "id": "realtime-empty",
                "type": "empty_state",
                "title": "正在等待课程知识点",
                "message": "播放课程后，已确认的实时字幕会形成带视频坐标的知识笔记。",
            }
        )

    final_children: list[dict[str, object]]
    if state.final_document_markdown:
        preview = state.final_document_markdown[:8_000]
        if len(state.final_document_markdown) > len(preview):
            preview += "\n\n> 内容较长，请使用可信文档下载查看完整 Markdown/JSON。"
        final_children = [
            {"id": "final-preview", "type": "safe_markdown", "markdown": preview}
        ]
    else:
        final_children = [
            {
                "id": "final-empty",
                "type": "empty_state",
                "title": "尚未生成最终整理",
                "message": "可手动生成临时版本；课程结束后会生成完整版本。",
            }
        ]

    history_items = [
        {
            "id": f"history-{index}"[:128],
            "primary": str(item.get("label", "文档版本"))[:2_000],
            "secondary": f"文档 ID：{str(item.get('document_id', ''))[:128]}",
        }
        for index, item in enumerate(state.history[-50:])
    ]
    history_children: list[dict[str, object]] = [
        {"id": "history-list", "type": "list", "items": history_items}
        if history_items
        else {
            "id": "history-empty",
            "type": "empty_state",
            "title": "暂无历史版本",
            "message": "生成后的临时版和完整版都会保留在这里。",
        }
    ]

    return {
        "schema_version": 1,
        "surface": "panel",
        "view_id": "course-organizer",
        "view_version": state.view_version,
        "root": {
            "id": "course-organizer-root",
            "type": "section",
            "title": "课程内容整理",
            "children": [
                {
                    "id": "output-language",
                    "type": "select",
                    "name": "output_language",
                    "label": "文档语言",
                    "options": [
                        {"id": "language-zh", "label": "中文", "value": "zh-CN"},
                        {"id": "language-en", "label": "English", "value": "en"},
                        {"id": "language-source", "label": "源语言", "value": "source"},
                    ],
                },
                {
                    "id": "set-language",
                    "type": "button",
                    "label": f"应用语言：{state.output_language}",
                    "action_id": "set-language",
                    "tone": "neutral",
                },
                {
                    "id": "generate-final",
                    "type": "button",
                    "label": "生成最终整理",
                    "action_id": "generate-final",
                    "tone": "primary",
                },
                {
                    "id": "course-tabs",
                    "type": "tabs",
                    "tabs": [
                        {
                            "id": "tab-realtime",
                            "label": "实时笔记",
                            "children": realtime_children,
                        },
                        {
                            "id": "tab-final",
                            "label": "最终文档",
                            "children": final_children,
                        },
                        {
                            "id": "tab-history",
                            "label": "历史版本",
                            "children": history_children,
                        },
                    ],
                },
            ],
        },
        "actions": [
            {"id": "set-language", "kind": "command", "command": "set_language"},
            {"id": "generate-final", "kind": "command", "command": "generate_final"},
            {"id": "retry-final", "kind": "command", "command": "retry_final"},
            {"id": "select-version", "kind": "command", "command": "select_version"},
        ],
    }


def _status_label(status: str) -> str:
    return {
        "ready": "实时笔记已就绪",
        "processing": "正在整理当前知识窗口",
        "degraded": "模型暂不可用，已使用规则笔记并等待重试",
    }.get(status, "实时笔记状态未知")


def _status_tone(status: str) -> str:
    return {"ready": "success", "processing": "info", "degraded": "warning"}.get(
        status,
        "neutral",
    )


__all__ = ["COURSE_COMMANDS", "build_course_view"]
