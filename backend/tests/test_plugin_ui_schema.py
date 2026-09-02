from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.media.repository import MediaRepository
from app.persistence.database import Database
from app.persistence.models import SessionRecord
from app.plugins.repository import PluginRepository
from app.plugins.capabilities import UIViewPublishInput
from app.plugins.ui_schema import (
    RepositoryUIViewCapabilityAdapter,
    RepositoryUIViewPublisher,
    UIViewValidationError,
    parse_plugin_view,
)


def envelope(root: dict[str, object], **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "surface": "panel",
        "view_id": "main",
        "view_version": 1,
        "root": root,
        "actions": [],
    }
    value.update(overrides)
    return value


def component(component_type: str) -> dict[str, object]:
    fixtures: dict[str, dict[str, object]] = {
        "text": {"id": "text-1", "type": "text", "text": "Hello"},
        "safe_markdown": {
            "id": "markdown-1",
            "type": "safe_markdown",
            "markdown": "**Safe** [docs](https://example.com)",
        },
        "card": {"id": "card-1", "type": "card", "title": "Card", "children": []},
        "section": {
            "id": "section-1",
            "type": "section",
            "title": "Section",
            "children": [],
        },
        "tabs": {
            "id": "tabs-1",
            "type": "tabs",
            "tabs": [{"id": "tab-1", "label": "One", "children": []}],
        },
        "list": {
            "id": "list-1",
            "type": "list",
            "items": [{"id": "item-1", "primary": "One", "secondary": "Detail"}],
        },
        "table": {
            "id": "table-1",
            "type": "table",
            "columns": [{"id": "name", "label": "Name"}],
            "rows": [{"name": "Ada"}],
        },
        "timeline": {
            "id": "timeline-1",
            "type": "timeline",
            "items": [{"id": "moment-1", "time_ms": 1000, "title": "Start"}],
        },
        "media_anchor": {
            "id": "anchor-1",
            "type": "media_anchor",
            "media_time_ms": 1000,
            "label": "Jump",
        },
        "badge": {"id": "badge-1", "type": "badge", "text": "Live", "tone": "info"},
        "metric": {"id": "metric-1", "type": "metric", "label": "Score", "value": "98"},
        "progress": {"id": "progress-1", "type": "progress", "label": "Done", "value": 50},
        "input": {"id": "input-1", "type": "input", "name": "title", "label": "Title"},
        "textarea": {
            "id": "textarea-1",
            "type": "textarea",
            "name": "notes",
            "label": "Notes",
        },
        "select": {
            "id": "select-1",
            "type": "select",
            "name": "choice",
            "label": "Choice",
            "options": [{"id": "option-1", "label": "One", "value": "one"}],
        },
        "checkbox": {
            "id": "checkbox-1",
            "type": "checkbox",
            "name": "accepted",
            "label": "Accept",
        },
        "button": {
            "id": "button-1",
            "type": "button",
            "label": "Run",
            "action_id": "run",
        },
        "confirmation": {
            "id": "confirm-1",
            "type": "confirmation",
            "title": "Confirm",
            "body": "Continue?",
            "confirm_action_id": "run",
        },
        "empty_state": {
            "id": "empty-1",
            "type": "empty_state",
            "title": "Nothing here",
        },
        "error_state": {
            "id": "error-1",
            "type": "error_state",
            "title": "Unavailable",
            "message": "Try later",
        },
    }
    return deepcopy(fixtures[component_type])


def test_closed_component_union_accepts_only_documented_panel_components() -> None:
    component_types = (
        "text",
        "safe_markdown",
        "card",
        "section",
        "tabs",
        "list",
        "table",
        "timeline",
        "media_anchor",
        "badge",
        "metric",
        "progress",
        "input",
        "textarea",
        "select",
        "checkbox",
        "button",
        "confirmation",
        "empty_state",
        "error_state",
    )
    for component_type in component_types:
        actions: list[dict[str, object]] = []
        if component_type in {"button", "confirmation"}:
            actions = [{"id": "run", "kind": "command", "command": "analyze"}]
        parsed = parse_plugin_view(
            envelope(component(component_type), actions=actions),
            allowed_commands=frozenset({"analyze"}),
            media_duration_ms=10_000,
        )
        assert parsed.root.type == component_type

    for hostile_type in ("script", "style", "iframe", "permission_prompt", "webview"):
        with pytest.raises(UIViewValidationError):
            parse_plugin_view(
                envelope({"id": "hostile", "type": hostile_type}),
                allowed_commands=frozenset(),
            )
    with pytest.raises(UIViewValidationError):
        parse_plugin_view(
            envelope(
                {
                    "id": "image-1",
                    "type": "text",
                    "text": "hello",
                    "url": "https://attacker.example/pixel",
                }
            ),
            allowed_commands=frozenset(),
        )


def test_overlay_allowlist_is_smaller_and_markdown_is_safe() -> None:
    for allowed in ("text", "safe_markdown", "media_anchor", "badge", "metric", "progress", "empty_state", "error_state"):
        parsed = parse_plugin_view(
            envelope(component(allowed), surface="overlay"),
            allowed_commands=frozenset(),
            media_duration_ms=10_000,
        )
        assert parsed.surface == "overlay"
    with pytest.raises(UIViewValidationError, match="overlay"):
        parse_plugin_view(
            envelope(component("input"), surface="overlay"),
            allowed_commands=frozenset(),
        )
    for markdown in (
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "[click](javascript:alert(1))",
        "[file](file:///etc/passwd)",
    ):
        hostile = component("safe_markdown")
        hostile["markdown"] = markdown
        with pytest.raises(UIViewValidationError, match="markdown"):
            parse_plugin_view(envelope(hostile), allowed_commands=frozenset())


def test_global_bounds_stable_ids_actions_and_media_anchors() -> None:
    nested: dict[str, object] = component("text")
    for depth in range(9):
        nested = {
            "id": f"section-{depth}",
            "type": "section",
            "title": "Nested",
            "children": [nested],
        }
    with pytest.raises(UIViewValidationError, match="depth"):
        parse_plugin_view(envelope(nested), allowed_commands=frozenset())

    oversized = component("text")
    oversized["text"] = "x" * 40_000
    with pytest.raises(UIViewValidationError):
        parse_plugin_view(envelope(oversized), allowed_commands=frozenset())

    too_many_nodes = {
        "id": "many",
        "type": "section",
        "title": "Many",
        "children": [
            {"id": f"text-{index}", "type": "text", "text": "x"}
            for index in range(201)
        ],
    }
    with pytest.raises(UIViewValidationError, match="nodes"):
        parse_plugin_view(envelope(too_many_nodes), allowed_commands=frozenset())

    duplicate_ids = {
        "id": "duplicate",
        "type": "section",
        "title": "Duplicates",
        "children": [
            {"id": "same", "type": "text", "text": "one"},
            {"id": "same", "type": "text", "text": "two"},
        ],
    }
    with pytest.raises(UIViewValidationError, match="unique"):
        parse_plugin_view(envelope(duplicate_ids), allowed_commands=frozenset())

    for row_count, option_count, action_count in ((101, 1, 1), (1, 51, 1), (1, 1, 33)):
        if row_count > 1:
            root = component("table")
            root["rows"] = [{"name": str(index)} for index in range(row_count)]
        elif option_count > 1:
            root = component("select")
            root["options"] = [
                {"id": f"option-{index}", "label": "Option", "value": str(index)}
                for index in range(option_count)
            ]
        else:
            root = component("text")
        actions = [
            {"id": f"action-{index}", "kind": "command", "command": "analyze"}
            for index in range(action_count)
        ]
        with pytest.raises(UIViewValidationError):
            parse_plugin_view(
                envelope(root, actions=actions),
                allowed_commands=frozenset({"analyze"}),
            )

    button = component("button")
    with pytest.raises(UIViewValidationError, match="action"):
        parse_plugin_view(envelope(button), allowed_commands=frozenset({"analyze"}))
    with pytest.raises(UIViewValidationError, match="declared"):
        parse_plugin_view(
            envelope(
                button,
                actions=[{"id": "run", "kind": "command", "command": "undeclared"}],
            ),
            allowed_commands=frozenset({"analyze"}),
        )
    anchor = component("media_anchor")
    anchor["media_time_ms"] = 20_000
    with pytest.raises(UIViewValidationError, match="media"):
        parse_plugin_view(
            envelope(anchor),
            allowed_commands=frozenset(),
            media_duration_ms=10_000,
        )


@pytest.fixture
def database() -> Database:
    value = Database("sqlite://")
    value.create_schema()
    try:
        yield value
    finally:
        value.dispose()


def seed(repository: PluginRepository, db_session: Session) -> str:
    repository.record_package(
        plugin_id="com.example.viewer",
        version="1.0.0",
        content_digest="sha256:" + "a" * 64,
        manifest_hash="sha256:" + "b" * 64,
        image_digest="sha256:" + "c" * 64,
        signature_status="verified",
        package_path="plugins/packages/viewer",
        publisher_id=None,
        manifest_json={"id": "com.example.viewer", "version": "1.0.0"},
    )
    db_session.add(
        SessionRecord(
            id="legacy-1",
            room_name="room-1",
            status="running",
            source_type="browser-tab",
            source_name="Tab",
            language="en-US",
        )
    )
    db_session.flush()
    return MediaRepository(db_session).ensure_legacy_session_bridge("legacy-1").id


def test_validated_persistence_increases_version_and_invalid_never_replaces(
    database: Database,
) -> None:
    with Session(database.engine) as db_session:
        repository = PluginRepository(db_session)
        media_session_id = seed(repository, db_session)
        publisher = RepositoryUIViewPublisher(repository)
        first = publisher.publish(
            plugin_id="com.example.viewer",
            version="1.0.0",
            media_session_id=media_session_id,
            raw_view=envelope(component("text")),
            allowed_commands=frozenset(),
        )
        assert first.view_version == 1
        with pytest.raises(UIViewValidationError):
            publisher.publish(
                plugin_id="com.example.viewer",
                version="1.0.0",
                media_session_id=media_session_id,
                raw_view=envelope(
                    {"id": "bad", "type": "iframe", "url": "https://evil.example"},
                    view_version=2,
                ),
                allowed_commands=frozenset(),
            )
        preserved = repository.get_view(
            plugin_id="com.example.viewer",
            version="1.0.0",
            media_session_id=media_session_id,
            surface="panel",
            view_id="main",
        )
        assert preserved is not None
        assert preserved.view_version == 1
        assert preserved.view_json["root"]["text"] == "Hello"

        replayed = publisher.publish(
            plugin_id="com.example.viewer",
            version="1.0.0",
            media_session_id=media_session_id,
            raw_view=envelope(component("text"), view_version=1),
            allowed_commands=frozenset(),
        )
        assert replayed.view_version == 1

        with pytest.raises(ValueError, match="version"):
            publisher.publish(
                plugin_id="com.example.viewer",
                version="1.0.0",
                media_session_id=media_session_id,
                raw_view=envelope(
                    {"id": "text", "type": "text", "text": "Changed"},
                    view_version=1,
                ),
                allowed_commands=frozenset(),
            )

        adapter = RepositoryUIViewCapabilityAdapter(
            repository,
            allowed_commands=lambda _plugin_id, _version: frozenset(),
        )
        result = asyncio.run(
            adapter(
                SimpleNamespace(
                    plugin_id="com.example.viewer",
                    plugin_version="1.0.0",
                    media_session_id=media_session_id,
                ),
                UIViewPublishInput(
                    surface="panel",
                    view_id="main",
                    view_version=2,
                    view={"id": "text-2", "type": "text", "text": "Updated"},
                ),
            )
        )
        assert result.accepted is True
        assert result.view_version == 2

        optional_result = asyncio.run(
            adapter(
                SimpleNamespace(
                    plugin_id="com.example.viewer",
                    plugin_version="1.0.0",
                    media_session_id=media_session_id,
                ),
                UIViewPublishInput(
                    surface="panel",
                    view_id="main",
                    view_version=3,
                    view=component("input"),
                ),
            )
        )
        assert optional_result.view_version == 3
        persisted = repository.get_view(
            plugin_id="com.example.viewer",
            version="1.0.0",
            media_session_id=media_session_id,
            surface="panel",
            view_id="main",
        )
        assert persisted is not None
        assert "placeholder" not in persisted.view_json["root"]
