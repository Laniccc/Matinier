from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from app.media.contracts import MediaEvent
from app.plugins.contracts import (
    JsonRpcErrorResponse,
    JsonRpcRequest,
    JsonRpcResponse,
    PluginManifest,
)
from app.plugins.sdk_schemas import generated_sdk_schemas
from app.plugins.ui_schema import PluginUIViewDocument


ROOT = Path(__file__).resolve().parents[2]
SDK = ROOT / "plugin-sdk"
DIAGNOSTIC = SDK / "examples" / "diagnostic"
COURSE_ORGANIZER = SDK / "examples" / "course-organizer"


def _load_diagnostic_module():
    path = DIAGNOSTIC / "plugin.py"
    spec = importlib.util.spec_from_file_location("diagnostic_plugin", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_published_json_schemas_are_generated_from_authoritative_models() -> None:
    generated = generated_sdk_schemas()
    assert set(generated) == {
        "json-rpc-envelope.schema.json",
        "media-event.schema.json",
        "plugin-capabilities.schema.json",
        "plugin-manifest.schema.json",
        "ui-view-document.schema.json",
    }
    for filename, schema in generated.items():
        published = json.loads((SDK / "schemas" / filename).read_text("utf-8"))
        assert published == schema
        assert published["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_sdk_examples_validate_against_the_source_contract_models() -> None:
    manifest = PluginManifest.model_validate_json(
        (DIAGNOSTIC / "plugin.json").read_text("utf-8")
    )
    assert manifest.id == "com.matinier.diagnostic"
    event = MediaEvent.model_validate(
        {
            "schema_version": 1,
            "event_id": "event-1",
            "session_id": "media-1",
            "sequence": 1,
            "event_type": "transcript.final",
            "finality": "final",
            "source": "host.transcription",
            "payload": {"text": "hello"},
            "created_at": "2026-08-28T00:00:00Z",
        }
    )
    assert event.sequence == 1
    view = _load_diagnostic_module().build_view("scope-1", 2, 1)
    assert PluginUIViewDocument.model_validate(view).view_version == 2
    course_manifest = PluginManifest.model_validate_json(
        (COURSE_ORGANIZER / "plugin.json").read_text("utf-8")
    )
    assert course_manifest.id == "com.matinier.course-organizer"


def test_stdlib_diagnostic_plugin_handles_lifecycle_events_ack_and_ui() -> None:
    module = _load_diagnostic_module()
    plugin = module.DiagnosticPlugin()

    initialized = plugin.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "plugin.initialize",
            "params": {
                "plugin_id": "com.matinier.diagnostic",
                "version": "1.0.0",
                "protocol_version": "1.0",
                "host_api": "1.0.0",
                "host_api_requirement": ">=1.0 <2.0",
            },
        }
    )
    assert JsonRpcResponse.model_validate(initialized[0]).result["host_api"] == "1.0.0"

    opened = plugin.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session.open",
            "params": {
                "media_session_id": "media-1",
                "session_scope": "scope-1234567890",
                "after_sequence": 0,
            },
        }
    )
    assert JsonRpcResponse.model_validate(opened[0]).result == {"opened": True}
    requests = [JsonRpcRequest.model_validate(item) for item in opened[1:]]
    assert {item.params["capability"] for item in requests} == {
        "state.get",
        "ui.publish",
    }

    delivered = plugin.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "event.batch",
            "params": {
                "media_session_id": "media-1",
                "session_scope": "scope-1234567890",
                "events": [
                    {
                        "sequence": 8,
                        "event_type": "transcript.final",
                        "payload": {"text": "hello"},
                    }
                ],
            },
        }
    )
    assert JsonRpcResponse.model_validate(delivered[0]).result == {
        "acknowledged_sequence": 8
    }
    assert any(
        JsonRpcRequest.model_validate(item).params["capability"] == "state.put"
        for item in delivered[1:]
    )

    heartbeat = plugin.handle(
        {"jsonrpc": "2.0", "id": 4, "method": "plugin.heartbeat", "params": {}}
    )
    assert JsonRpcResponse.model_validate(heartbeat[0]).result["ok"] is True

    malformed = plugin.handle({"jsonrpc": "2.0", "id": 5, "method": "unknown", "params": {}})
    assert JsonRpcErrorResponse.model_validate(malformed[0]).error.code == "plugin.protocol.method_not_found"


def test_package_script_help_is_side_effect_free() -> None:
    script = ROOT / "backend" / "scripts" / "package_diagnostic_plugin.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--private-key" in result.stdout
    assert "--output" in result.stdout


def test_course_plugin_source_uses_only_the_public_stdlib_runtime_boundary() -> None:
    source_files = [COURSE_ORGANIZER / "plugin.py"] + list(
        (COURSE_ORGANIZER / "course_organizer").glob("*.py")
    )
    assert source_files[0].is_file()
    combined = "\n".join(path.read_text("utf-8") for path in source_files)
    assert "from app" not in combined
    assert "import app" not in combined
    assert "pydantic" not in combined
    assert "sqlalchemy" not in combined
