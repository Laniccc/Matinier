from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.plugins.manifest import load_manifest
from app.settings import PROJECT_ROOT, Settings


COURSE_ORGANIZER_ID = "com.matinier.course-organizer"
MEETING_ASSISTANT_ID = "com.matinier.meeting-assistant"
MEETING_ASSISTANT_SOURCE_DIR = PROJECT_ROOT / "plugin-sdk/examples/meeting-assistant"
MEETING_ASSISTANT_PERMISSIONS = (
    "meeting.execution.cancel", "meeting.execution.input", "meeting.execution.submit",
    "meeting.mark.write", "meeting.operation.query", "meeting.state.query", "meeting.turn.submit",
    "state.get", "state.put", "ui.publish",
)
COURSE_ORGANIZER_SOURCE_DIR = (
    PROJECT_ROOT / "plugin-sdk" / "examples" / "course-organizer"
).resolve()
COURSE_ORGANIZER_PERMISSIONS = (
    "delivery.prepare",
    "delivery.query",
    "document.publish",
    "model.invoke",
    "state.get",
    "state.put",
    "ui.publish",
)


@dataclass(frozen=True, slots=True)
class BuiltinPluginDescriptor:
    """Server-owned metadata for one allowlisted first-party plugin."""

    plugin_id: str
    display_name: str
    description: str
    version: str
    source_id: str
    source_dir: Path
    permissions: tuple[str, ...]
    dynamic_build_available: bool


class BuiltinPluginRegistry:
    """Read-only allowlist of first-party plugins known to this Host build."""

    def __init__(self, settings: Settings) -> None:
        course_source = COURSE_ORGANIZER_SOURCE_DIR.resolve()
        course_manifest, _raw_manifest = load_manifest(
            (course_source / "plugin.json").read_bytes()
        )
        if course_manifest.id != COURSE_ORGANIZER_ID:
            raise ValueError(
                "course organizer manifest id does not match the allowlist"
            )
        if course_manifest.permissions != COURSE_ORGANIZER_PERMISSIONS:
            raise ValueError(
                "course organizer manifest permissions do not match the allowlist"
            )
        course = BuiltinPluginDescriptor(
            plugin_id=course_manifest.id,
            display_name=course_manifest.name,
            description=(
                "从课程字幕生成带视频坐标的实时知识笔记，"
                "并在手动触发或会话结束后整理课程知识点。"
            ),
            version=course_manifest.version,
            source_id="course-organizer",
            source_dir=course_source,
            permissions=course_manifest.permissions,
            dynamic_build_available=(
                settings.plugin_framework_enabled
                and settings.plugin_builtin_build_enabled
            ),
        )
        meeting_source = MEETING_ASSISTANT_SOURCE_DIR.resolve()
        meeting_manifest, _raw = load_manifest((meeting_source / "plugin.json").read_bytes())
        if meeting_manifest.id != MEETING_ASSISTANT_ID:
            raise ValueError("meeting assistant manifest id does not match the allowlist")
        if meeting_manifest.permissions != MEETING_ASSISTANT_PERMISSIONS:
            raise ValueError("meeting assistant manifest permissions do not match the allowlist")
        meeting = BuiltinPluginDescriptor(
            plugin_id=meeting_manifest.id, display_name=meeting_manifest.name,
            description="显式开启会议分析，查看会议重点、证据和执行历史；经主程序确认后私密问答或执行任务。",
            version=meeting_manifest.version, source_id="meeting-assistant", source_dir=meeting_source,
            permissions=meeting_manifest.permissions,
            dynamic_build_available=course.dynamic_build_available,
        )
        self._descriptors = {course.plugin_id: course, meeting.plugin_id: meeting}

    def list(self) -> tuple[BuiltinPluginDescriptor, ...]:
        return tuple(
            self._descriptors[plugin_id]
            for plugin_id in sorted(self._descriptors)
        )

    def require(self, plugin_id: str) -> BuiltinPluginDescriptor:
        try:
            return self._descriptors[plugin_id]
        except KeyError as error:
            raise LookupError("unknown built-in plugin") from error
