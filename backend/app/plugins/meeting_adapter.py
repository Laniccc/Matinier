"""Short, caller-owned meeting admissions; never run an engine under the Broker lock."""
from sqlalchemy import select

from app.assistant.plugin_contracts import (
    MeetingAskInput, MeetingExecuteInput, MeetingMarkInput, MeetingInputInput, MeetingCancelInput,
    MeetingStateQueryInput, MeetingOperationQueryInput, MeetingOperationAccepted, MeetingQueryOutput,
    MeetingCommandInput,
)
from app.assistant.plugin_repository import MEETING_PLUGIN_ID, MeetingPluginDenied
from app.assistant.plugin_service import MeetingPluginReadService
from app.persistence.models import AssistantActionIntentRecord, MeetingPluginOperationRecord
from app.plugins.capabilities import CapabilitySpec
from app.plugins.host_action_contracts import HostActionScope
from app.plugins.host_actions import ACTION_CAPABILITIES, capability_grant_scope, token_hash
from app.plugins.repository import PluginRepository


CAPABILITIES = (
    ("meeting.state.query", "read", MeetingStateQueryInput, MeetingQueryOutput),
    ("meeting.operation.query", "read", MeetingOperationQueryInput, MeetingQueryOutput),
    ("meeting.turn.submit", "local_write", MeetingAskInput, MeetingOperationAccepted),
    ("meeting.mark.write", "local_write", MeetingMarkInput, MeetingOperationAccepted),
    ("meeting.execution.submit", "external_write", MeetingExecuteInput, MeetingOperationAccepted),
    ("meeting.execution.input", "local_write", MeetingInputInput, MeetingOperationAccepted),
    ("meeting.execution.cancel", "local_write", MeetingCancelInput, MeetingOperationAccepted),
)


async def _session_adapter_required(*_args):
    raise RuntimeError("meeting capability requires a scoped Host adapter")


def register_meeting_capabilities(registry, *, timeout_seconds=10):
    for name, effect, input_model, output_model in CAPABILITIES:
        write = effect != "read"
        registry.register_host(CapabilitySpec(name=name, version="1.0", effect=effect,
            input_model=input_model, output_model=output_model, timeout_seconds=timeout_seconds,
            requires_action_grant=effect == "external_write", supports_idempotency=write,
            supports_reconciliation=write), None,
            reconciler=_session_adapter_required if write else None)


class MeetingCapabilityAdapter:
    def __init__(self, db, operations, *, is_current):
        self.db, self.operations, self.is_current = db, operations, is_current

    def preflight(self, context, capability, value):
        if not self.is_current() or context.plugin_id != MEETING_PLUGIN_ID or not context.media_session_id:
            raise MeetingPluginDenied("meeting process scope is stale or invalid")
        if not (self.operations.settings.assistant_enabled and self.operations.settings.plugin_framework_enabled):
            raise MeetingPluginDenied("meeting capabilities are unavailable")
        MeetingPluginReadService(self.db).require_scope(context.media_session_id, context.plugin_id, context.plugin_version)
        if capability not in PluginRepository(self.db).list_base_permissions(plugin_id=context.plugin_id, version=context.plugin_version):
            raise MeetingPluginDenied("meeting capability permission is missing")
        if not isinstance(value, MeetingCommandInput):
            return {}
        scope = self.intent_scope(context, capability, value)
        return capability_grant_scope(scope) if scope.action in {"meeting.execute", "meeting.input"} else {}

    def intent_scope(self, context, capability, value):
        row = self.db.scalar(select(AssistantActionIntentRecord).where(
            AssistantActionIntentRecord.token_hash == token_hash(value.intent_token)))
        if row is None:
            raise MeetingPluginDenied("confirmed intent is missing")
        scope = HostActionScope.model_validate(row.scope_json)
        if (scope.plugin_id, scope.plugin_version, scope.media_session_id) != (
                context.plugin_id, context.plugin_version, context.media_session_id
        ) or scope.source != "plugin" or ACTION_CAPABILITIES[scope.action] != capability:
            raise MeetingPluginDenied("confirmed intent is outside this capability scope")
        if isinstance(value, MeetingMarkInput) and scope.action != f"meeting.mark.{value.operation}":
            raise MeetingPluginDenied("mark action differs from confirmed intent")
        return scope

    def invoke(self, context, capability, value, decision):
        self.preflight(context, capability, value)
        service = MeetingPluginReadService(self.db)
        kwargs = dict(media_session_id=context.media_session_id, plugin_id=context.plugin_id,
            plugin_version=context.plugin_version, query=value)
        if capability == "meeting.state.query":
            return MeetingQueryOutput(data=service.query(**kwargs))
        if capability == "meeting.operation.query":
            return MeetingQueryOutput(data=service.operation(**kwargs))
        scope = self.intent_scope(context, capability, value)
        if scope.action in {"meeting.execute", "meeting.input"}:
            self.operations.require_capability_grant(self.db, scope)
            if decision.grant_id != scope.capability_grant_id or dict(decision.authorized_scope) != capability_grant_scope(scope):
                raise MeetingPluginDenied("Broker authorization differs from Host confirmation")
        operation = self.operations.admit(self.db, plugin_id=context.plugin_id,
            plugin_version=context.plugin_version, media_session_id=context.media_session_id,
            action=scope.action, command=value.model_dump(mode="json"))
        return MeetingOperationAccepted(operation_id=operation.id)

    def reconcile(self, context, capability, value):
        # Looking up the durable identity is the only allowed recovery action.
        self.preflight(context, capability, value)
        scope = self.intent_scope(context, capability, value)
        row = self.db.scalar(select(MeetingPluginOperationRecord).where(
            MeetingPluginOperationRecord.plugin_id == context.plugin_id,
            MeetingPluginOperationRecord.plugin_version == context.plugin_version,
            MeetingPluginOperationRecord.media_session_id == context.media_session_id,
            MeetingPluginOperationRecord.client_request_id == value.request_id,
            MeetingPluginOperationRecord.action == scope.action))
        if row is None or self.operations.scope_for(self.db, row) != scope:
            raise MeetingPluginDenied("no accepted operation to reconcile")
        return MeetingOperationAccepted(operation_id=row.id)
