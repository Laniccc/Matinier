from __future__ import annotations

import re
from typing import Annotated, Callable, Literal, Protocol, TypeAlias
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.plugins.capabilities import UIViewPublishInput, UIViewPublishOutput
from app.plugins.repository import PluginRepository


MAX_DEPTH = 8
MAX_NODES = 200
MAX_TOTAL_STRING_CHARS = 64_000
MAX_TABLE_ROWS = 100
MAX_OPTIONS = 50
MAX_ACTIONS = 32

OVERLAY_COMPONENTS = frozenset(
    {
        "text",
        "safe_markdown",
        "media_anchor",
        "badge",
        "metric",
        "progress",
        "empty_state",
        "error_state",
    }
)

_STABLE_ID = r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"
_NAME = r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")
_RAW_HTML = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")


class UIViewValidationError(ValueError):
    pass


class _UIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Component(_UIModel):
    id: str = Field(pattern=_STABLE_ID)


class TextComponent(_Component):
    type: Literal["text"]
    text: str = Field(max_length=32_000)


class SafeMarkdownComponent(_Component):
    type: Literal["safe_markdown"]
    markdown: str = Field(max_length=32_000)

    @field_validator("markdown")
    @classmethod
    def safe_markdown(cls, value: str) -> str:
        if _RAW_HTML.search(value):
            raise ValueError("unsafe markdown raw HTML is forbidden")
        for match in _MARKDOWN_LINK.finditer(value):
            destination = match.group(1)
            parsed = urlsplit(destination)
            if parsed.scheme.casefold() != "https" or not parsed.hostname:
                raise ValueError("unsafe markdown link is forbidden")
        lowered = value.casefold()
        if "javascript:" in lowered or "data:" in lowered or "file:" in lowered:
            raise ValueError("unsafe markdown link is forbidden")
        return value


class CardComponent(_Component):
    type: Literal["card"]
    title: str | None = Field(default=None, max_length=500)
    children: tuple["PluginComponent", ...] = Field(default=(), max_length=500)


class SectionComponent(_Component):
    type: Literal["section"]
    title: str = Field(max_length=500)
    children: tuple["PluginComponent", ...] = Field(default=(), max_length=500)


class TabItem(_UIModel):
    id: str = Field(pattern=_STABLE_ID)
    label: str = Field(min_length=1, max_length=200)
    children: tuple["PluginComponent", ...] = Field(default=(), max_length=500)


class TabsComponent(_Component):
    type: Literal["tabs"]
    tabs: tuple[TabItem, ...] = Field(min_length=1, max_length=20)


class ListItem(_UIModel):
    id: str = Field(pattern=_STABLE_ID)
    primary: str = Field(min_length=1, max_length=2_000)
    secondary: str | None = Field(default=None, max_length=4_000)


class ListComponent(_Component):
    type: Literal["list"]
    items: tuple[ListItem, ...] = Field(default=(), max_length=200)


class TableColumn(_UIModel):
    id: str = Field(pattern=_NAME)
    label: str = Field(min_length=1, max_length=200)


class TableComponent(_Component):
    type: Literal["table"]
    columns: tuple[TableColumn, ...] = Field(min_length=1, max_length=20)
    rows: tuple[dict[str, str], ...] = Field(default=(), max_length=MAX_TABLE_ROWS)

    @model_validator(mode="after")
    def rows_match_columns(self) -> "TableComponent":
        columns = {column.id for column in self.columns}
        if any(not set(row).issubset(columns) for row in self.rows):
            raise ValueError("table row contains an unknown column")
        if any(len(value) > 4_000 for row in self.rows for value in row.values()):
            raise ValueError("table cell string exceeds limit")
        return self


class TimelineItem(_UIModel):
    id: str = Field(pattern=_STABLE_ID)
    time_ms: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=500)
    body: str | None = Field(default=None, max_length=4_000)


class TimelineComponent(_Component):
    type: Literal["timeline"]
    items: tuple[TimelineItem, ...] = Field(default=(), max_length=200)


class MediaAnchorComponent(_Component):
    type: Literal["media_anchor"]
    media_time_ms: int = Field(ge=0)
    label: str = Field(min_length=1, max_length=500)


class BadgeComponent(_Component):
    type: Literal["badge"]
    text: str = Field(min_length=1, max_length=200)
    tone: Literal["neutral", "info", "success", "warning", "danger"] = "neutral"


class MetricComponent(_Component):
    type: Literal["metric"]
    label: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=500)
    detail: str | None = Field(default=None, max_length=1_000)


class ProgressComponent(_Component):
    type: Literal["progress"]
    label: str = Field(min_length=1, max_length=200)
    value: float = Field(ge=0, le=100)


class InputComponent(_Component):
    type: Literal["input"]
    name: str = Field(pattern=_NAME)
    label: str = Field(min_length=1, max_length=200)
    placeholder: str | None = Field(default=None, max_length=500)


class TextareaComponent(_Component):
    type: Literal["textarea"]
    name: str = Field(pattern=_NAME)
    label: str = Field(min_length=1, max_length=200)
    placeholder: str | None = Field(default=None, max_length=500)
    rows: int = Field(default=4, ge=2, le=20)


class SelectOption(_UIModel):
    id: str = Field(pattern=_STABLE_ID)
    label: str = Field(min_length=1, max_length=200)
    value: str = Field(max_length=500)


class SelectComponent(_Component):
    type: Literal["select"]
    name: str = Field(pattern=_NAME)
    label: str = Field(min_length=1, max_length=200)
    options: tuple[SelectOption, ...] = Field(min_length=1, max_length=MAX_OPTIONS)


class CheckboxComponent(_Component):
    type: Literal["checkbox"]
    name: str = Field(pattern=_NAME)
    label: str = Field(min_length=1, max_length=500)
    checked: bool = False


class ButtonComponent(_Component):
    type: Literal["button"]
    label: str = Field(min_length=1, max_length=200)
    action_id: str = Field(pattern=_STABLE_ID)
    tone: Literal["neutral", "primary", "danger"] = "neutral"


class ConfirmationComponent(_Component):
    type: Literal["confirmation"]
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=4_000)
    confirm_action_id: str = Field(pattern=_STABLE_ID)
    cancel_action_id: str | None = Field(default=None, pattern=_STABLE_ID)


class EmptyStateComponent(_Component):
    type: Literal["empty_state"]
    title: str = Field(min_length=1, max_length=500)
    message: str | None = Field(default=None, max_length=4_000)


class ErrorStateComponent(_Component):
    type: Literal["error_state"]
    title: str = Field(min_length=1, max_length=500)
    message: str = Field(min_length=1, max_length=4_000)


PluginComponent: TypeAlias = Annotated[
    TextComponent
    | SafeMarkdownComponent
    | CardComponent
    | SectionComponent
    | TabsComponent
    | ListComponent
    | TableComponent
    | TimelineComponent
    | MediaAnchorComponent
    | BadgeComponent
    | MetricComponent
    | ProgressComponent
    | InputComponent
    | TextareaComponent
    | SelectComponent
    | CheckboxComponent
    | ButtonComponent
    | ConfirmationComponent
    | EmptyStateComponent
    | ErrorStateComponent,
    Field(discriminator="type"),
]


for recursive_model in (CardComponent, SectionComponent, TabItem, TabsComponent):
    recursive_model.model_rebuild(_types_namespace={"PluginComponent": PluginComponent})


class UIViewAction(_UIModel):
    id: str = Field(pattern=_STABLE_ID)
    kind: Literal["command"]
    command: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")


class PluginUIViewDocument(_UIModel):
    schema_version: Literal[1]
    surface: Literal["panel", "overlay"]
    view_id: str = Field(pattern=_STABLE_ID)
    view_version: int = Field(ge=1)
    root: PluginComponent
    actions: tuple[UIViewAction, ...] = Field(default=(), max_length=MAX_ACTIONS)


def parse_plugin_view(
    raw_view: dict[str, object],
    *,
    allowed_commands: frozenset[str],
    media_duration_ms: int | None = None,
) -> PluginUIViewDocument:
    try:
        document = PluginUIViewDocument.model_validate(raw_view)
    except ValidationError as error:
        messages = "; ".join(str(item["msg"]) for item in error.errors()[:4])
        raise UIViewValidationError(messages or "plugin UI schema validation failed") from error
    canonical = document.model_dump(mode="json")
    component_nodes = _component_nodes(canonical["root"], depth=1)
    if component_nodes.count > MAX_NODES:
        raise UIViewValidationError("plugin UI exceeds maximum nodes")
    if component_nodes.max_depth > MAX_DEPTH:
        raise UIViewValidationError("plugin UI exceeds maximum depth")
    if _total_string_chars(canonical) > MAX_TOTAL_STRING_CHARS:
        raise UIViewValidationError("plugin UI exceeds total string limit")
    if len(component_nodes.ids) != len(set(component_nodes.ids)):
        raise UIViewValidationError("plugin UI component IDs must be unique")
    component_types = component_nodes.types
    if document.surface == "overlay" and not component_types.issubset(OVERLAY_COMPONENTS):
        raise UIViewValidationError("overlay contains a component that is not allowed")

    action_ids = {action.id for action in document.actions}
    if len(action_ids) != len(document.actions):
        raise UIViewValidationError("plugin UI action IDs must be unique")
    for action in document.actions:
        if action.command not in allowed_commands:
            raise UIViewValidationError("plugin UI action command was not declared")
    for action_reference in _action_references(canonical["root"]):
        if action_reference not in action_ids:
            raise UIViewValidationError("plugin UI action reference does not exist")
    if media_duration_ms is not None:
        if media_duration_ms < 0:
            raise ValueError("media duration must not be negative")
        if any(value > media_duration_ms for value in _media_anchors(canonical["root"])):
            raise UIViewValidationError("plugin UI media anchor exceeds media duration")
    return document


class RepositoryUIViewPublisher:
    def __init__(self, repository: PluginRepository) -> None:
        self._repository = repository

    def publish(
        self,
        *,
        plugin_id: str,
        version: str,
        media_session_id: str,
        raw_view: dict[str, object],
        allowed_commands: frozenset[str],
        media_duration_ms: int | None = None,
    ):
        document = parse_plugin_view(
            raw_view,
            allowed_commands=allowed_commands,
            media_duration_ms=media_duration_ms,
        )
        return self._repository.publish_view(
            plugin_id=plugin_id,
            version=version,
            media_session_id=media_session_id,
            surface=document.surface,
            view_id=document.view_id,
            view_version=document.view_version,
            view_json=document.model_dump(mode="json", exclude_none=True),
        )


class UIViewCapabilityContext(Protocol):
    plugin_id: str
    plugin_version: str
    media_session_id: str | None


class RepositoryUIViewCapabilityAdapter:
    """Capability adapter that validates before delegating to durable persistence."""

    def __init__(
        self,
        repository: PluginRepository,
        *,
        allowed_commands: Callable[[str, str], frozenset[str]],
        media_duration_ms: Callable[[str], int | None] | None = None,
    ) -> None:
        self._publisher = RepositoryUIViewPublisher(repository)
        self._allowed_commands = allowed_commands
        self._media_duration_ms = media_duration_ms

    async def __call__(
        self,
        context: UIViewCapabilityContext,
        value: UIViewPublishInput,
    ) -> UIViewPublishOutput:
        if context.media_session_id is None:
            raise UIViewValidationError("ui.publish requires a MediaSession scope")
        record = self._publisher.publish(
            plugin_id=context.plugin_id,
            version=context.plugin_version,
            media_session_id=context.media_session_id,
            raw_view={
                "schema_version": 1,
                "surface": value.surface,
                "view_id": value.view_id,
                "view_version": value.view_version,
                "root": value.view,
                "actions": list(value.actions),
            },
            allowed_commands=self._allowed_commands(
                context.plugin_id,
                context.plugin_version,
            ),
            media_duration_ms=(
                self._media_duration_ms(context.media_session_id)
                if self._media_duration_ms is not None
                else None
            ),
        )
        return UIViewPublishOutput(accepted=True, view_version=record.view_version)


class _NodeStats:
    def __init__(self) -> None:
        self.count = 0
        self.max_depth = 0
        self.ids: list[str] = []
        self.types: set[str] = set()


def _component_nodes(value: object, *, depth: int) -> _NodeStats:
    stats = _NodeStats()

    def visit(item: object, current_depth: int) -> None:
        if isinstance(item, dict):
            if isinstance(item.get("type"), str):
                stats.count += 1
                stats.max_depth = max(stats.max_depth, current_depth)
                stats.types.add(item["type"])
                if isinstance(item.get("id"), str):
                    stats.ids.append(item["id"])
                current_depth += 1
            for nested in item.values():
                visit(nested, current_depth)
        elif isinstance(item, list):
            for nested in item:
                visit(nested, current_depth)

    visit(value, depth)
    return stats


def _total_string_chars(value: object) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(len(str(key)) + _total_string_chars(item) for key, item in value.items())
    if isinstance(value, list):
        return sum(_total_string_chars(item) for item in value)
    return 0


def _action_references(value: object) -> tuple[str, ...]:
    references: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"action_id", "confirm_action_id", "cancel_action_id"} and isinstance(item, str):
                references.append(item)
            else:
                references.extend(_action_references(item))
    elif isinstance(value, list):
        for item in value:
            references.extend(_action_references(item))
    return tuple(references)


def _media_anchors(value: object) -> tuple[int, ...]:
    anchors: list[int] = []
    if isinstance(value, dict):
        if value.get("type") == "media_anchor" and isinstance(value.get("media_time_ms"), int):
            anchors.append(value["media_time_ms"])
        for item in value.values():
            anchors.extend(_media_anchors(item))
    elif isinstance(value, list):
        for item in value:
            anchors.extend(_media_anchors(item))
    return tuple(anchors)


__all__ = [
    "PluginComponent",
    "PluginUIViewDocument",
    "RepositoryUIViewCapabilityAdapter",
    "RepositoryUIViewPublisher",
    "UIViewValidationError",
    "parse_plugin_view",
]
