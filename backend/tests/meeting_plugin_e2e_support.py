"""Temporary-Host acceptance harness. All model/task boundaries are local fakes.

Shared by pytest and the developer-only Docker smoke; never imported by app code.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import importlib
import sys
import time
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.assistant.bootstrap import build_assistant_runtime
from app.assistant.models import ContextSnapshot
from app.assistant.planner import PlanningObservation
from app.persistence.database import Database
from app.persistence.models import AssistantExecutionRecord, MeetingPluginOperationRecord
from app.plugins.bootstrap import PluginHostRuntime
from app.plugins.signing import assets_digest, canonical_json_bytes, signed_material
from app.plugins.ui_schema import parse_plugin_view
from app.settings import Settings
from app.text_processing.provider import StructuredCompletionResult
from meeting_plugin_fakes import ControlledLinear, ControlledMeetingModel, seed_meeting_history
from test_private_meeting_agent_core import _TaskLifecyclePlanner

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = "com.matinier.meeting-assistant"
VERSION = "1.0.0"
ADMIN = {"X-Plugin-Admin-Token": "meeting-smoke-only"}
ORIGIN = "http://127.0.0.1:3000"


class AcceptanceSettings(Settings):
    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        # Ignore ambient environment too, including production/provider flags.
        return (init_settings,)


def wait_for(predicate, label, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        # Bounded observation polling, never a substitute for race-test gates.
        time.sleep(0.02)
    raise AssertionError("Acceptance condition timed out: " + label)


class MeetingProvider:
    provider_name, model = "fake-meeting", "acceptance-only"

    def __init__(self):
        self.calls = []
        self.ask_entered, self.ask_release = asyncio.Event(), asyncio.Event()
        self.ask_release.set()

    async def complete_structured(self, request):
        payload = request.input_payload
        self.calls.append(str(payload.get("profile")))
        refs = payload["available_evidence_refs"]
        if "role" in payload:
            decision = {"summary": "Test branch evidence checked", "payload": {},
                "evidence_refs": [refs[0]], "model_call_count": 1, "blocks_external_write": False}
        elif payload["profile"] == "fast_turn":
            self.ask_entered.set()
            await self.ask_release.wait()
            decision = {"kind": "respond", "decision_summary": "Answer using supplied evidence",
                "claims": [{"text": "Acceptance answer: release notes", "evidence_refs": [refs[0]]}]}
        elif str(payload["goal"]).startswith("Need input"):
            decision = {"kind": "needs_input", "decision_summary": "Explicit clarification",
                "question": "Which release?", "evidence_refs": [refs[0]]}
        else:
            outcome = await _TaskLifecyclePlanner("meeting-candidate", 1).decide(
                snapshot=ContextSnapshot.model_validate(payload["snapshot"]),
                observations=[PlanningObservation.model_validate(o) for o in payload["observations"]])
            decision = outcome.decision.model_dump(mode="json")
        return StructuredCompletionResult(content=json.dumps(decision), finish_reason="stop")


class Harness:
    def __init__(self, root, *, containers=None, course_provider=None, lose_response=False, seed=True, real_docker=False):
        self.settings = AcceptanceSettings(_env_file=None, database_url=f"sqlite:///{(root / 'meeting.db').as_posix()}",
            data_dir=root / "data", plugin_admin_token=ADMIN["X-Plugin-Admin-Token"],
            assistant_enabled=True, plugin_framework_enabled=True, task_system_provider="fake",
            linear_team_id="fake-team", linear_api_key=None, deepseek_api_key="unused-fake-only",
            livekit_url="ws://127.0.0.1:7880", livekit_room_name="meeting-acceptance-only",
            livekit_api_key="test-key", livekit_api_secret="test-secret-that-is-at-least-32-bytes",
            meeting_state_batch_segments=1, meeting_state_poll_interval_ms=100,
            plugin_rpc_timeout_seconds=5, plugin_shutdown_timeout_seconds=2,
            assistant_reconciliation_delays_seconds="0.05,0.1", log_level="ERROR")
        self.database = Database(self.settings.database_url)
        self.database.create_schema()
        if seed:
            seed_meeting_history(self.database)
        self.provider = MeetingProvider()
        self.extractor = ControlledMeetingModel()
        self.linear = ControlledLinear(lose_response=lose_response)

        def assistant(settings, database, **kwargs):
            runtime = build_assistant_runtime(settings, database, provider=self.provider,
                task_adapter=self.linear, **kwargs)
            runtime.projector._extractor = self.extractor
            return runtime

        def plugins(settings, database, **kwargs):
            options = {} if containers is None else {"container_runtime": containers}
            if containers is not None and not real_docker:
                options["peer_factory"] = lambda process: process.peer
            return PluginHostRuntime(settings, database, structured_provider=course_provider, **options, **kwargs)

        # Only the external boundaries are replaced; main creates and shares its
        # real HostActions, policy, Broker operations and lifecycle gates.
        # main exposes an ASGI app at import time. Prevent that unused instance
        # from loading the checkout's .env before constructing our explicit app.
        if "app.main" not in sys.modules:
            from app.settings import get_settings
            bootstrap_settings = self.settings.model_copy(update={"assistant_enabled": False,
                "plugin_framework_enabled": False, "database_url": "sqlite://"})
            with patch("app.settings.get_settings", return_value=bootstrap_settings):
                main = importlib.import_module("app.main")
            main.get_settings = get_settings
        else:
            main = sys.modules["app.main"]
        with patch("app.main.build_assistant_runtime", assistant), patch("app.main.build_plugin_host_runtime", plugins):
            self.app = main.create_app(settings=self.settings, database=self.database)
        self.runtime = self.app.state.plugin_host_runtime
        self.assistant = self.app.state.assistant_runtime
        self.client = None

    def __enter__(self):
        self.client = TestClient(self.app).__enter__()
        self.headers = {"Origin": ORIGIN, "X-Assistant-UI": "1"}
        response = self.client.post("/api/assistant-actions/ui-context", headers=self.headers, json={})
        assert response.status_code == 200, response.text
        self.headers["X-Assistant-UI-Nonce"] = response.json()["ui_nonce"]
        return self

    def __exit__(self, *args):
        self.client.__exit__(*args)

    def install(self, package, plugin=PLUGIN):
        response = self.client.post("/api/plugins/packages:inspect", headers={**ADMIN,
            "Content-Type": "application/vnd.matinier.plugin+zip"}, content=package.read_bytes())
        assert response.status_code == 200, response.text
        inspection = response.json()
        response = self.client.post("/api/plugins/installations", headers=ADMIN, json={
            "ticket_id": inspection["ticket_id"], "accepted_permissions": inspection["permissions"],
            "trust_publisher": True, "approved_publisher_fingerprint": inspection["publisher_fingerprint"]})
        assert response.status_code == 201, response.text
        response = self.client.post(f"/api/plugins/{plugin}/enable", headers=ADMIN)
        assert response.status_code == 200 and response.json()["runtime_status"] == "ready", response.text

    def bind(self, session="meeting-a"):
        response = self.client.get(f"/api/sessions/{session}/media-session")
        assert response.status_code == 200, response.text
        return response.json()["media_session_id"]

    def view(self, media="media-a", plugin=PLUGIN):
        views = self.client.get(f"/api/media-sessions/{media}/plugin-views").json()
        return next((v for v in views if v["plugin_id"] == plugin), None)

    def history(self, media="media-a", **params):
        response = self.client.get(f"/api/media-sessions/{media}/assistant-history", params=params)
        assert response.status_code == 200, response.text
        result = response.json()
        parse_plugin_view(result["ui_view"], allowed_commands=frozenset())
        return result

    def prepare(self, action, arguments=None, *, media="media-a", request_id=None):
        view = self.view(media)
        response = self.client.post(f"/api/media-sessions/{media}/assistant-actions/prepare",
            headers=self.headers, json={"action": action, "request_id": request_id or uuid.uuid4().hex,
                "plugin_version": view["plugin_version"], "view_version": view["view_version"],
                "arguments": arguments or {}})
        assert response.status_code == 200, response.text
        return response.json()

    def confirm(self, preview, confirmed=True, *, media="media-a"):
        response = self.client.post(f"/api/media-sessions/{media}/assistant-actions/confirm", headers=self.headers,
            json={"preview_id": preview["preview_id"], "preview_hash": preview["preview_hash"], "confirmed": confirmed})
        assert response.status_code == 200, response.text
        assert "intent_token" not in response.text and '"command"' not in response.text
        return response.json()

    def action(self, name, arguments=None):
        return self.confirm(self.prepare(name, arguments))

    def operation(self, identifier):
        with self.database.session() as db:
            op = db.get(MeetingPluginOperationRecord, identifier)
            execution = db.get(AssistantExecutionRecord, op.execution_id) if op.execution_id else None
            return {"status": op.status, "execution_id": op.execution_id,
                "execution_status": execution.status if execution else None,
                "state_version": execution.state_version if execution else None,
                "error": op.error_code}

    def settled(self, admitted):
        assert admitted["status"] == "accepted", admitted
        return wait_for(lambda: (o if (o := self.operation(admitted["operation_id"]))["status"]
            not in {"accepted", "running"} else None), "durable operation completion")


def meeting_package(root, key=None):
    """A real signed/verified package with a deliberately fake OCI transport."""
    image = b"meeting-fake-image"
    manifest = json.loads((ROOT / "plugin-sdk/examples/meeting-assistant/plugin.json").read_text("utf-8"))
    manifest["image_digest"] = "sha256:" + hashlib.sha256(image).hexdigest()
    key = key or Ed25519PrivateKey.generate()
    digest = assets_digest({})
    envelope = {"schema_version": 1, "algorithm": "Ed25519", "publisher": manifest["publisher"],
        "public_key": base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,
            serialization.PublicFormat.Raw)).decode(), "assets_digest": digest,
        "signature": base64.b64encode(key.sign(signed_material(manifest, manifest["image_digest"], digest))).decode()}
    target = root / "meeting.plugin.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("plugin.json", canonical_json_bytes(manifest))
        archive.writestr("image.tar", image)
        archive.writestr("signature.json", canonical_json_bytes(envelope))
    return target
