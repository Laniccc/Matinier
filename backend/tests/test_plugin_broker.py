from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.media.repository import MediaRepository
from app.persistence.database import Database
from app.persistence.models import (
    PluginAuditEventRecord,
    PluginCapabilityInvocationRecord,
    SessionRecord,
)
from app.plugins.broker import (
    BrokerConnection,
    CapabilityBroker,
    CapabilityExecutionContext,
    NetworkResponse,
    OutcomeUnknownError,
)
from app.plugins.bootstrap import PluginHostRuntime
from app.plugins.container_runtime import PluginIdentity
from app.plugins.capabilities import (
    ActionExecuteInput,
    ActionExecuteOutput,
    CapabilityRegistry,
    CapabilitySpec,
    DeliveryPrepareInput,
    DeliveryPrepareOutput,
    DeliveryQueryInput,
    DeliveryQueryOutput,
    MediaQueryInput,
    MediaQueryOutput,
    ModelInvokeInput,
    ModelInvokeOutput,
    NetworkFetchInput,
    NetworkFetchOutput,
    StateGetInput,
    StateGetOutput,
    StatePutInput,
    StatePutOutput,
    UIViewPublishInput,
    UIViewPublishOutput,
)
from app.plugins.document_contracts import (
    PluginDocumentPublishInput,
    PluginDocumentPublishOutput,
)
from app.plugins.permissions import PermissionDeniedError, PermissionEvaluator
from app.plugins.repository import PluginRepository
from app.settings import Settings
from app.text_processing.provider import StructuredCompletionResult


NOW = datetime.now(UTC)


@pytest.fixture
def database() -> Database:
    value = Database("sqlite://")
    value.create_schema()
    try:
        yield value
    finally:
        value.dispose()


class Fakes:
    def __init__(self, repository: PluginRepository) -> None:
        self.repository = repository
        self.model_active = 0
        self.model_peak = 0
        self.fetches: list[str] = []
        self.ui_payloads: list[UIViewPublishInput] = []
        self.action_attempts = 0
        self.action_reconciliations = 0
        self.delivery_prepares = 0
        self.document_publications = 0
        self.network_response = NetworkResponse(
            status=200,
            mime_type="application/json",
            body=b'{"ok":true}',
            headers={"x-api-key": "must-not-leak"},
        )
        self.network_responses: dict[str, NetworkResponse] = {}

    async def media(
        self,
        context: CapabilityExecutionContext,
        value: MediaQueryInput,
    ) -> MediaQueryOutput:
        events = MediaRepository(self.repository._db).list_events_after(
            context.media_session_id or "",
            after_sequence=value.after_sequence,
            limit=value.limit,
        )
        allowed = set(value.event_types)
        return MediaQueryOutput(
            events=[
                {
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "payload": event.payload_json,
                }
                for event in events
                if event.event_type in allowed
            ]
        )

    async def model(
        self,
        _context: CapabilityExecutionContext,
        value: ModelInvokeInput,
    ) -> ModelInvokeOutput:
        self.model_active += 1
        self.model_peak = max(self.model_peak, self.model_active)
        await asyncio.sleep(0)
        self.model_active -= 1
        return ModelInvokeOutput(
            output={"text": value.user_prompt.upper()},
            provider="fake",
            model="fake-structured",
            finish_reason="stop",
            output_tokens=3,
        )

    async def resolve(self, hostname: str) -> list[str]:
        if hostname == "localhost":
            return ["127.0.0.1"]
        return ["93.184.216.34"]

    async def network(
        self,
        _context: CapabilityExecutionContext,
        value: NetworkFetchInput,
    ) -> NetworkResponse:
        url = str(value.url)
        self.fetches.append(url)
        return self.network_responses.get(url, self.network_response)

    async def ui(
        self,
        _context: CapabilityExecutionContext,
        value: UIViewPublishInput,
    ) -> UIViewPublishOutput:
        self.ui_payloads.append(value)
        return UIViewPublishOutput(accepted=True, view_version=value.view_version)

    async def action(
        self,
        _context: CapabilityExecutionContext,
        _value: ActionExecuteInput,
    ) -> ActionExecuteOutput:
        self.action_attempts += 1
        raise OutcomeUnknownError("remote timeout after send")

    async def reconcile(
        self,
        _context: CapabilityExecutionContext,
        _value: ActionExecuteInput,
        _idempotency_key: str,
    ) -> ActionExecuteOutput:
        self.action_reconciliations += 1
        return ActionExecuteOutput(status="completed", external_id="TASK-1")

    async def prepare(
        self,
        _context: CapabilityExecutionContext,
        value: DeliveryPrepareInput,
    ) -> DeliveryPrepareOutput:
        self.delivery_prepares += 1
        return DeliveryPrepareOutput(
            package_id="package-1",
            package_version=1,
            content_hash="d" * 64,
            source_language="en-US",
            target_languages=(value.output_language,),
            final_sequence=value.final_sequence,
        )

    async def query(
        self,
        _context: CapabilityExecutionContext,
        _value: DeliveryQueryInput,
    ) -> DeliveryQueryOutput:
        return DeliveryQueryOutput(
            package_id="package-1",
            package_version=1,
            content_hash="d" * 64,
            items=(),
            next_after_item=None,
        )

    async def document(
        self,
        _context: CapabilityExecutionContext,
        value: PluginDocumentPublishInput,
    ) -> PluginDocumentPublishOutput:
        self.document_publications += 1
        return PluginDocumentPublishOutput(
            document_id="document-1",
            document_version=1,
            identity_key=value.identity_key,
            content_hash="e" * 64,
            language=value.language,
            trigger=value.trigger,
            completeness=value.completeness,
        )


def install(repository: PluginRepository) -> str:
    package = repository.record_package(
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
    repository.ensure_installation(
        plugin_id="com.example.viewer",
        preferred_version="1.0.0",
        status="enabled",
    )
    repository.replace_base_permissions(
        plugin_id="com.example.viewer",
        version="1.0.0",
        permissions=(
            "media.query",
            "model.invoke",
            "network.fetch",
            "state.get",
            "state.put",
            "ui.publish",
            "action.execute",
            "delivery.prepare",
            "delivery.query",
            "document.publish",
        ),
    )
    return package.id


def seed_media(db_session: Session) -> str:
    db_session.add(
        SessionRecord(
            id="legacy-1",
            room_name="room-1",
            status="running",
            source_type="browser-tab",
            source_name="Shared tab",
            language="en-US",
        )
    )
    db_session.flush()
    media = MediaRepository(db_session).ensure_legacy_session_bridge("legacy-1")
    repository = MediaRepository(db_session)
    repository.append_event(
        media_session_id=media.id,
        event_type="transcript.final",
        finality="final",
        source="caption.runtime",
        payload={"text": "allowed"},
    )
    repository.append_event(
        media_session_id=media.id,
        event_type="playback.state",
        finality="final",
        source="player.runtime",
        payload={"state": "not-requested"},
    )
    return media.id


def registry(fakes: Fakes) -> CapabilityRegistry:
    value = CapabilityRegistry()

    def add(
        name: str,
        effect: str,
        input_model: type[Any],
        output_model: type[Any],
        handler: Any,
        *,
        action: bool = False,
        reconciliation: bool = False,
        reconciler: Any = None,
        idempotent: bool = False,
    ) -> None:
        value.register_host(
            CapabilitySpec(
                name=name,
                version="1.0",
                effect=effect,  # type: ignore[arg-type]
                input_model=input_model,
                output_model=output_model,
                timeout_seconds=2,
                requires_action_grant=action,
                supports_idempotency=action or idempotent,
                supports_reconciliation=reconciliation,
            ),
            handler,
            reconciler=reconciler,
        )

    add("media.query", "read", MediaQueryInput, MediaQueryOutput, fakes.media)
    add("model.invoke", "read", ModelInvokeInput, ModelInvokeOutput, fakes.model)
    add("network.fetch", "network", NetworkFetchInput, NetworkFetchOutput, fakes.network)
    add("state.get", "read", StateGetInput, StateGetOutput, None)
    add("state.put", "local_write", StatePutInput, StatePutOutput, None)
    add("ui.publish", "local_write", UIViewPublishInput, UIViewPublishOutput, fakes.ui)
    add(
        "delivery.prepare",
        "local_write",
        DeliveryPrepareInput,
        DeliveryPrepareOutput,
        None,
        idempotent=True,
    )
    add(
        "delivery.query",
        "read",
        DeliveryQueryInput,
        DeliveryQueryOutput,
        None,
    )
    add(
        "document.publish",
        "local_write",
        PluginDocumentPublishInput,
        PluginDocumentPublishOutput,
        None,
        idempotent=True,
    )
    add(
        "action.execute",
        "external_write",
        ActionExecuteInput,
        ActionExecuteOutput,
        fakes.action,
        action=True,
        reconciliation=True,
        reconciler=fakes.reconcile,
    )
    return value


def broker(
    repository: PluginRepository,
    media_session_id: str,
    fakes: Fakes,
    *,
    model_semaphore: asyncio.Semaphore | None = None,
) -> CapabilityBroker:
    return CapabilityBroker(
        connection=BrokerConnection(
            plugin_id="com.example.viewer",
            plugin_version="1.0.0",
            generation=1,
            session_scopes={"opaque-scope": media_session_id},
        ),
        repository=repository,
        registry=registry(fakes),
        permission_evaluator=PermissionEvaluator(),
        network_resolver=fakes.resolve,
        state_quota_bytes=512,
        model_concurrency=1,
        model_semaphore=model_semaphore,
        delivery_adapter=fakes,
        document_adapter=fakes.document,
    )


def test_local_write_capabilities_require_idempotency_and_recheck_permission(
    database: Database,
) -> None:
    async def scenario() -> None:
        with Session(database.engine) as db_session:
            repository = PluginRepository(db_session)
            install(repository)
            media_session_id = seed_media(db_session)
            fakes = Fakes(repository)
            subject = broker(repository, media_session_id, fakes)
            prepare = {
                "trigger": "manual",
                "output_language": "zh-CN",
                "final_sequence": 2,
            }
            with pytest.raises(ValueError, match="idempotency"):
                await subject.invoke(
                    "delivery.prepare",
                    prepare,
                    session_scope="opaque-scope",
                )
            prepared = await subject.invoke(
                "delivery.prepare",
                prepare,
                session_scope="opaque-scope",
                idempotency_key="prepare-1",
            )
            repeated = await subject.invoke(
                "delivery.prepare",
                prepare,
                session_scope="opaque-scope",
                idempotency_key="prepare-1",
            )
            assert prepared == repeated
            assert fakes.delivery_prepares == 1

            document = {
                "identity_key": "course-notes:zh-CN",
                "schema_name": "matinier.course-notes",
                "schema_version": "1.0",
                "language": "zh-CN",
                "trigger": "manual",
                "completeness": "interim",
                "source_package_id": "package-1",
                "content": {"title": "Course"},
                "markdown": "# Course\n",
                "evidence_refs": [],
            }
            with pytest.raises(ValueError, match="idempotency"):
                await subject.invoke(
                    "document.publish",
                    document,
                    session_scope="opaque-scope",
                )

            secret = "transcript-secret-sentinel"
            await subject.invoke(
                "model.invoke",
                {
                    "input_category": "session_transcript",
                    "system_prompt": "Return JSON.",
                    "user_prompt": secret,
                    "input_payload": {"text": secret},
                    "response_format": "json_object",
                    "max_output_tokens": 32,
                    "timeout_seconds": 1,
                },
            )
            audits = list(db_session.scalars(select(PluginAuditEventRecord)))
            assert secret not in str([item.payload_json for item in audits])
            repository.revoke_base_permission(
                plugin_id="com.example.viewer",
                version="1.0.0",
                permission="model.invoke",
            )
            with pytest.raises(PermissionDeniedError, match="installation"):
                await subject.invoke(
                    "model.invoke",
                    {
                        "input_category": "public",
                        "system_prompt": "Return JSON.",
                        "user_prompt": "after revoke",
                        "input_payload": {},
                        "response_format": "json_object",
                        "max_output_tokens": 32,
                        "timeout_seconds": 1,
                    },
                )

    asyncio.run(scenario())


def test_production_registry_specs_and_install_permission_rejection(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="test-key",
        livekit_api_secret="test-secret-that-is-at-least-32-bytes",
        livekit_room_name="test-room",
        database_url="sqlite://",
        data_dir=tmp_path,
    )
    database = Database("sqlite://")
    runtime = PluginHostRuntime(settings, database)
    expected = {
        "media.query": ("read", False),
        "model.invoke": ("read", False),
        "delivery.prepare": ("local_write", True),
        "delivery.query": ("read", False),
        "document.publish": ("local_write", True),
        "state.get": ("read", False),
        "state.put": ("local_write", False),
        "ui.publish": ("local_write", False),
        "network.fetch": ("network", False),
        "meeting.state.query": ("read", False),
        "meeting.operation.query": ("read", False),
        "meeting.turn.submit": ("local_write", True),
        "meeting.mark.write": ("local_write", True),
        "meeting.execution.submit": ("external_write", True),
        "meeting.execution.input": ("local_write", True),
        "meeting.execution.cancel": ("local_write", True),
    }
    try:
        assert set(runtime.capability_registry._bindings) == set(expected)  # noqa: SLF001
        for name, (effect, idempotent) in expected.items():
            spec = runtime.capability_registry.require(name).spec
            assert spec.effect == effect
            assert spec.supports_idempotency is idempotent
            if name.startswith("meeting."):
                assert spec.requires_action_grant is (name == "meeting.execution.submit")
                assert spec.supports_reconciliation is idempotent

        class StoreMustNotRun:
            called = False

            async def confirm_install(self, *_args, **_kwargs):
                self.called = True
                raise AssertionError("unknown permission reached package installation")

        store = StoreMustNotRun()
        runtime.package_store = store  # type: ignore[assignment]
        with pytest.raises(ValueError, match="unsupported capability"):
            asyncio.run(
                runtime.confirm_install(
                    ticket_id="ticket-1",
                    accepted_permissions=("unknown.capability",),
                    trust_publisher=False,
                    approved_publisher_fingerprint=None,
                )
            )
        assert store.called is False
    finally:
        database.dispose()


def test_model_concurrency_is_shared_across_brokers() -> None:
    database = Database("sqlite://")
    database.create_schema()

    async def scenario() -> None:
        with database.session() as db_session:
            repository = PluginRepository(db_session)
            install(repository)
            media_session_id = seed_media(db_session)
            fakes = Fakes(repository)
            shared = asyncio.Semaphore(1)
            first = broker(
                repository,
                media_session_id,
                fakes,
                model_semaphore=shared,
            )
            second = broker(
                repository,
                media_session_id,
                fakes,
                model_semaphore=shared,
            )
            request = {
                "input_category": "public",
                "system_prompt": "Return JSON.",
                "user_prompt": "hello",
                "input_payload": {},
                "response_format": "json_object",
                "max_output_tokens": 32,
                "timeout_seconds": 1,
            }
            await asyncio.gather(
                first.invoke("model.invoke", request),
                second.invoke("model.invoke", request),
            )
            assert fakes.model_peak == 1

    try:
        asyncio.run(scenario())
    finally:
        database.dispose()


def grant(
    repository: PluginRepository,
    media_session_id: str,
    capability: str,
    effect: str,
    scope: dict[str, object],
) -> None:
    repository.create_capability_grant(
        plugin_id="com.example.viewer",
        version="1.0.0",
        media_session_id=media_session_id,
        capability=capability,
        effect=effect,
        scope=scope,
        expires_at=NOW + timedelta(minutes=30),
    )


def test_media_model_state_ui_and_audit_boundaries(database: Database) -> None:
    async def scenario() -> None:
        with Session(database.engine) as db_session:
            repository = PluginRepository(db_session)
            install(repository)
            media_session_id = seed_media(db_session)
            grant(
                repository,
                media_session_id,
                "media.query",
                "read",
                {"event_types": ["transcript.final"], "max_calls": 5},
            )
            fakes = Fakes(repository)
            subject = broker(repository, media_session_id, fakes)

            media = await subject.invoke(
                "media.query",
                {
                    "after_sequence": 0,
                    "limit": 20,
                    "event_types": ["transcript.final"],
                },
                session_scope="opaque-scope",
            )
            assert [event["event_type"] for event in media["events"]] == [
                "transcript.final"
            ]
            assert "not-requested" not in str(media)

            model_results = await asyncio.gather(
                *(
                    subject.invoke(
                        "model.invoke",
                        {
                            "input_category": "public",
                            "system_prompt": "Return JSON.",
                            "user_prompt": f"hello {index}",
                            "input_payload": {"index": index},
                            "response_format": "json_object",
                            "max_output_tokens": 32,
                            "timeout_seconds": 1,
                        },
                    )
                    for index in range(3)
                )
            )
            assert len(model_results) == 3
            assert fakes.model_peak == 1
            with pytest.raises(ValueError):
                await subject.invoke(
                    "model.invoke",
                    {
                        "input_category": "public",
                        "system_prompt": "Return JSON.",
                        "user_prompt": "too much",
                        "input_payload": {},
                        "response_format": "json_object",
                        "max_output_tokens": 999_999,
                        "timeout_seconds": 1,
                    },
                )

            created = await subject.invoke(
                "state.put",
                {"key": "summary", "value": {"text": "v1"}, "expected_version": 0},
                session_scope="opaque-scope",
            )
            assert created["version"] == 1
            loaded = await subject.invoke(
                "state.get",
                {"key": "summary"},
                session_scope="opaque-scope",
            )
            assert loaded == {"value": {"text": "v1"}, "version": 1}
            with pytest.raises(ValueError):
                await subject.invoke(
                    "state.put",
                    {"key": "large", "value": {"text": "x" * 600}, "expected_version": 0},
                    session_scope="opaque-scope",
                )
            with pytest.raises(PermissionDeniedError, match="scope"):
                await subject.invoke(
                    "state.get",
                    {"key": "summary"},
                    session_scope="foreign-scope",
                )

            published = await subject.invoke(
                "ui.publish",
                {
                    "surface": "panel",
                    "view_id": "main",
                    "view_version": 1,
                    "view": {"type": "text", "text": "ready"},
                },
                session_scope="opaque-scope",
            )
            assert published["accepted"] is True
            assert len(fakes.ui_payloads) == 1

            audits = list(db_session.scalars(select(PluginAuditEventRecord)))
            assert len(audits) >= 2 * 7
            assert all(item.plugin_id == "com.example.viewer" for item in audits)

    asyncio.run(scenario())


def test_network_ssrf_redirect_mime_size_and_grant_scope(database: Database) -> None:
    async def scenario() -> None:
        with Session(database.engine) as db_session:
            repository = PluginRepository(db_session)
            install(repository)
            media_session_id = seed_media(db_session)
            grant(
                repository,
                media_session_id,
                "network.fetch",
                "network",
                {
                    "destinations": ["api.example.com"],
                    "methods": ["GET"],
                    "max_calls": 10,
                },
            )
            fakes = Fakes(repository)
            subject = broker(repository, media_session_id, fakes)

            response = await subject.invoke(
                "network.fetch",
                {
                    "url": "https://api.example.com/data",
                    "method": "GET",
                    "accepted_mime_types": ["application/json"],
                    "max_response_bytes": 128,
                },
                session_scope="opaque-scope",
            )
            assert response["status"] == 200
            assert "must-not-leak" not in str(response)
            with pytest.raises(PermissionDeniedError):
                await subject.invoke(
                    "network.fetch",
                    {
                        "url": "https://other.example/data",
                        "method": "GET",
                        "accepted_mime_types": ["application/json"],
                        "max_response_bytes": 128,
                    },
                    session_scope="opaque-scope",
                )
            with pytest.raises(ValueError, match="HTTPS"):
                await subject.invoke(
                    "network.fetch",
                    {
                        "url": "http://api.example.com/data",
                        "method": "GET",
                        "accepted_mime_types": ["application/json"],
                        "max_response_bytes": 128,
                    },
                    session_scope="opaque-scope",
                )
            with pytest.raises(ValueError, match="public"):
                await subject.invoke(
                    "network.fetch",
                    {
                        "url": "https://localhost/data",
                        "method": "GET",
                        "accepted_mime_types": ["application/json"],
                        "max_response_bytes": 128,
                    },
                    session_scope="opaque-scope",
                )

            fakes.network_response = NetworkResponse(200, "text/html", b"<html>", {})
            with pytest.raises(ValueError, match="MIME"):
                await subject.invoke(
                    "network.fetch",
                    {
                        "url": "https://api.example.com/data",
                        "method": "GET",
                        "accepted_mime_types": ["application/json"],
                        "max_response_bytes": 128,
                    },
                    session_scope="opaque-scope",
                )

            fakes.network_response = NetworkResponse(
                200, "application/json", b"x" * 129, {}
            )
            with pytest.raises(ValueError, match="byte limit"):
                await subject.invoke(
                    "network.fetch",
                    {
                        "url": "https://api.example.com/large",
                        "method": "GET",
                        "accepted_mime_types": ["application/json"],
                        "max_response_bytes": 128,
                    },
                    session_scope="opaque-scope",
                )

            fakes.network_responses = {
                "https://api.example.com/start": NetworkResponse(
                    302,
                    "text/plain",
                    b"",
                    {"location": "/final"},
                ),
                "https://api.example.com/final": NetworkResponse(
                    200,
                    "application/json",
                    b'{"redirected":true}',
                    {},
                ),
                "https://api.example.com/private": NetworkResponse(
                    302,
                    "text/plain",
                    b"",
                    {"location": "https://localhost/private"},
                ),
            }
            redirected = await subject.invoke(
                "network.fetch",
                {
                    "url": "https://api.example.com/start",
                    "method": "GET",
                    "accepted_mime_types": ["application/json"],
                    "max_response_bytes": 128,
                },
                session_scope="opaque-scope",
            )
            assert "redirected" in redirected["body"]
            with pytest.raises(ValueError, match="public"):
                await subject.invoke(
                    "network.fetch",
                    {
                        "url": "https://api.example.com/private",
                        "method": "GET",
                        "accepted_mime_types": ["application/json"],
                        "max_response_bytes": 128,
                    },
                    session_scope="opaque-scope",
                )

    asyncio.run(scenario())


def test_external_write_is_recorded_then_reconciled_without_direct_retry(
    database: Database,
) -> None:
    async def scenario() -> None:
        with Session(database.engine) as db_session:
            repository = PluginRepository(db_session)
            install(repository)
            media_session_id = seed_media(db_session)
            grant(
                repository,
                media_session_id,
                "action.execute",
                "external_write",
                {"actions": ["create_task"], "max_calls": 1},
            )
            fakes = Fakes(repository)
            subject = broker(repository, media_session_id, fakes)
            params = {"action": "create_task", "payload": {"title": "Follow up"}}

            with pytest.raises(OutcomeUnknownError):
                await subject.invoke(
                    "action.execute",
                    params,
                    session_scope="opaque-scope",
                    idempotency_key="action-1",
                )
            pending = db_session.scalar(select(PluginCapabilityInvocationRecord))
            assert pending is not None and pending.outcome_unknown is True
            assert fakes.action_attempts == 1

            reconciled = await subject.invoke(
                "action.execute",
                params,
                session_scope="opaque-scope",
                idempotency_key="action-1",
            )
            assert reconciled == {"status": "completed", "external_id": "TASK-1"}
            assert fakes.action_attempts == 1
            assert fakes.action_reconciliations == 1

            with pytest.raises(ValueError, match="idempotency"):
                await subject.invoke(
                    "action.execute",
                    params,
                    session_scope="opaque-scope",
                )

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
def test_runtime_model_wait_does_not_block_caption_or_plugin_writes(
    tmp_path,
    outcome: str,
) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'model-wait.db').as_posix()}")
    database.create_schema()
    with database.session() as db_session:
        install(PluginRepository(db_session))
        media_session_id = seed_media(db_session)
        db_session.commit()

    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class WaitingProvider:
            provider_name = "waiting"
            model = "test"

            async def complete_structured(self, _request):
                started.set()
                await release.wait()
                if outcome == "failure":
                    raise RuntimeError("simulated provider failure")
                return StructuredCompletionResult(content='{"notes":[]}', finish_reason="stop")

        class Peer:
            def __init__(self) -> None:
                self.handlers: dict[str, Any] = {}

            def register_handler(self, method, handler, **_kwargs) -> None:
                self.handlers[method] = handler

        runtime = PluginHostRuntime(
            Settings(
                _env_file=None,
                livekit_url="ws://127.0.0.1:7880",
                livekit_api_key="test-key",
                livekit_api_secret="test-secret-that-is-at-least-32-bytes",
                livekit_room_name="test-room",
                data_dir=tmp_path,
            ),
            database,
            structured_provider=WaitingProvider(),
        )
        identity = PluginIdentity("com.example.viewer", "1.0.0")
        peer = Peer()
        runtime._configure_peer(identity, peer, 1)
        runtime._scope_maps[identity]["scope"] = media_session_id
        request = asyncio.create_task(
            peer.handlers["capability.invoke"]({
                "capability": "model.invoke",
                "session_scope": "scope",
                "input": {
                    "input_category": "session_transcript",
                    "system_prompt": "Return JSON.",
                    "user_prompt": "Extract notes.",
                    "input_payload": {},
                    "response_format": "json_object",
                    "max_output_tokens": 128,
                    "timeout_seconds": 10,
                },
            })
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            with database.session() as db_session:
                db_session.execute(text("PRAGMA busy_timeout=100"))
                # A separate connection represents the ongoing caption worker.
                legacy = db_session.get(SessionRecord, "legacy-1")
                assert legacy is not None
                legacy.source_name = "Captions continue during model request"
                db_session.commit()
                pending = db_session.scalar(select(PluginCapabilityInvocationRecord))
                assert pending is not None and pending.status == "pending"
            await asyncio.wait_for(runtime._record_status(identity, "ready", {}), timeout=1)
            if outcome == "cancel":
                request.cancel()
            else:
                release.set()
            if outcome == "success":
                assert (await request)["output"] == {"notes": []}
            else:
                with pytest.raises(asyncio.CancelledError if outcome == "cancel" else RuntimeError):
                    await request
            await asyncio.wait_for(runtime._record_status(identity, "ready", {}), timeout=1)
            assert not runtime._database_write_lock.locked()
            with database.session() as db_session:
                invocation = db_session.scalar(select(PluginCapabilityInvocationRecord))
                assert invocation is not None
                assert invocation.status == {
                    "success": "completed", "failure": "failed", "cancel": "pending"
                }[outcome]
        finally:
            release.set()
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)

    try:
        asyncio.run(scenario())
    finally:
        database.dispose()
