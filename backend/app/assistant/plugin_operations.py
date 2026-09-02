"""Atomic admission into existing executions. No awaits or nested write sessions."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.assistant.context import ContextBuilder
from app.assistant.plugin_repository import (
    MEETING_PLUGIN_ID, MeetingPluginConflict, MeetingPluginDenied, MeetingPluginRepository, canonical_hash,
)
from app.assistant.plugin_service import MeetingPluginReadService
from app.assistant.repository import AssistantRepository
from app.assistant.state_machine import is_terminal_execution_status
from app.meeting_state.candidates import caption_evidence_message
from app.meeting_state.repository import MeetingStateRepository
from app.persistence.models import (
    AssistantActionIntentRecord, AssistantExecutionRecord, MeetingPluginOperationRecord,
    MeetingPluginSessionRecord, utc_now,
)
from app.plugins.host_actions import ACTION_CAPABILITIES, ACTION_MODELS, HostActions, _aware, begin_write, normalize_arguments, token_hash
from app.plugins.host_action_contracts import HostActionPrepareInput, HostActionScope
from app.plugins.repository import PluginRepository


class MeetingPluginOperations:
    def __init__(self, actions: HostActions):
        self.actions = actions
        self.database = actions.database
        self.settings = actions.settings

    def admit(self, db: Session, *, plugin_id: str, plugin_version: str,
              media_session_id: str, action: str, command: dict[str, object],
              from_host_history: bool = False) -> MeetingPluginOperationRecord:
        if action not in ACTION_MODELS or plugin_id != MEETING_PLUGIN_ID:
            raise MeetingPluginDenied("invalid meeting action owner")
        validated = ACTION_MODELS[action].model_validate(command)
        arguments = normalize_arguments(action, validated.model_dump(mode="json", exclude={"request_id", "intent_token"}))
        begin_write(db)
        intent = db.scalar(select(AssistantActionIntentRecord).where(
            AssistantActionIntentRecord.token_hash == token_hash(validated.intent_token),
        ))
        if intent is None:
            raise MeetingPluginDenied("intent is unavailable")
        scope = HostActionScope.model_validate(intent.scope_json)
        if (scope.plugin_id, scope.plugin_version, scope.media_session_id, scope.action) != (
            plugin_id, plugin_version, media_session_id, action,
        ) or (scope.source == "host_history" and not from_host_history):
            raise MeetingPluginDenied("intent scope does not match the operation")
        existing = db.scalar(select(MeetingPluginOperationRecord).where(
            MeetingPluginOperationRecord.plugin_id == plugin_id,
            MeetingPluginOperationRecord.plugin_version == plugin_version,
            MeetingPluginOperationRecord.media_session_id == media_session_id,
            MeetingPluginOperationRecord.client_request_id == validated.request_id,
        ))
        if existing is not None:
            if existing.action != action or existing.request_payload_json != arguments:
                raise MeetingPluginConflict("request ID already binds different arguments")
            if existing.intent_id != intent.id:
                raise MeetingPluginDenied("operation belongs to another confirmed intent")
            # Reading the accepted identity is not a second consumption or a
            # permission to rerun it; the worker/final tool gate checks authority.
            return existing
        expected_hash = canonical_hash({"request_id": validated.request_id, "arguments": arguments})
        if scope.payload_hash != expected_hash:
            raise MeetingPluginDenied("confirmed action arguments changed")
        rebuilt, _, _ = self.actions.build_scope(db, media_id=media_session_id,
            request=HostActionPrepareInput(action=action, request_id=validated.request_id,
                plugin_version=plugin_version, arguments=arguments), actor_id=scope.actor_id,
            history_cancel=scope.source == "host_history")
        rebuilt = rebuilt.model_copy(update={"capability_grant_id": scope.capability_grant_id})
        if rebuilt != scope:
            raise MeetingPluginDenied("confirmed action scope changed")
        if scope.action in {"meeting.execute", "meeting.input"}:
            self.require_capability_grant(db, scope)
        repo = MeetingPluginRepository(db)
        repo.consume_intent(token_hash=intent.token_hash, expected_scope=scope, now=self.actions.now())
        operation = repo.accept_operation(scope=scope, client_request_id=validated.request_id,
            request_payload=arguments, intent_id=intent.id)
        assistant = AssistantRepository(db)
        if action in {"meeting.ask", "meeting.execute"}:
            grant_id = None
            snapshot_id = None
            if action == "meeting.execute":
                snapshot = ContextBuilder(db).build_sync(
                    session_id=scope.legacy_session_id, goal=arguments["message"],
                    actor_id=scope.actor_id, mark_ids=arguments["mark_ids"],
                    candidate_ids=arguments["candidate_ids"], persist=True,
                )
                snapshot_id = snapshot.snapshot_id
                grant = assistant.create_grant(session_id=scope.legacy_session_id,
                    actor_id=scope.actor_id, goal=arguments["message"], capabilities=("task.create",),
                    resource_scope={"linear_team_id": scope.team_id},
                    candidate_ids=tuple(scope.candidate_revisions), max_side_effects=scope.max_side_effects,
                    expires_at=intent.expires_at, linear_team_id=scope.team_id)
                grant_id = grant.id
            execution = assistant.create_execution(
                execution_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"meeting-operation:{operation.id}")),
                session_id=scope.legacy_session_id, profile="fast_turn" if action == "meeting.ask" else "action_run",
                goal=arguments["message"], client_request_id=f"meeting:{operation.id}",
                snapshot_id=snapshot_id, grant_id=grant_id, use_savepoint=False,
                budget={"max_steps": self.settings.assistant_action_max_steps,
                    "max_model_calls": self.settings.assistant_action_max_model_calls,
                    "max_planning_rounds": self.settings.assistant_action_max_planning_rounds},
            )
            if action == "meeting.execute":
                assistant.append_event(
                    execution_id=execution.id,
                    event_type="action.context_frozen",
                    phase=execution.status,
                    summary="Action Run context was frozen",
                    payload={
                        "snapshot_id": snapshot.snapshot_id,
                        "meeting_state_version": snapshot.meeting_state_version,
                        "evidence_count": len(snapshot.evidence_refs),
                    },
                    state_version=execution.state_version,
                )
            operation.execution_id = execution.id
        elif action == "meeting.input":
            execution = MeetingPluginReadService(db).require_execution(scope.legacy_session_id, arguments["execution_id"])
            assistant.append_observation(execution_id=execution.id, source="user", source_ref=operation.id,
                observation={"input": arguments["input"], "actor_id": scope.actor_id})
            assistant.transition_execution(execution.id, expected_version=arguments["expected_state_version"],
                target_status="planning", event_type="action.input_received", summary="Action Run received user input",
                payload={"client_operation_id": operation.id}, result={"input_received": True})
            operation.execution_id = execution.id
        elif action == "meeting.cancel":
            self._cancel(db, operation, scope, arguments)
        else:
            self._mark(db, operation, scope, arguments)
        db.flush()
        return operation

    def scope_for(self, db: Session, operation: MeetingPluginOperationRecord) -> HostActionScope:
        intent = db.get(AssistantActionIntentRecord, operation.intent_id) if operation.intent_id else None
        if intent is None or intent.status != "consumed":
            raise MeetingPluginDenied("operation intent is unavailable")
        scope = HostActionScope.model_validate(intent.scope_json)
        if (scope.plugin_id, scope.plugin_version, scope.media_session_id, scope.legacy_session_id,
                scope.authority_epoch, scope.action) != (
                operation.plugin_id, operation.plugin_version, operation.media_session_id,
                operation.legacy_session_id, operation.authority_epoch, operation.action):
            raise MeetingPluginDenied("operation ownership changed")
        return scope

    def require_authority(self, db: Session, operation: MeetingPluginOperationRecord) -> HostActionScope:
        scope = self.scope_for(db, operation)
        if scope.source == "host_history" and scope.action == "meeting.cancel":
            return scope
        if not (self.actions.accepting and self.settings.assistant_enabled and self.settings.plugin_framework_enabled):
            raise MeetingPluginDenied("meeting execution is unavailable")
        if operation.status in {"failed", "cancelled"}:
            raise MeetingPluginDenied("operation is no longer executable")
        service = MeetingPluginReadService(db)
        service.require_scope(scope.media_session_id, scope.plugin_id, scope.plugin_version)
        if self.actions.runtime_check is not None:
            self.actions.runtime_check(db, scope.media_session_id, scope.action, scope.plugin_version)
        row = db.get(MeetingPluginSessionRecord, scope.legacy_session_id, populate_existing=True)
        if row is None or row.authority_epoch != scope.authority_epoch:
            raise MeetingPluginDenied("operation authority was revoked")
        permissions = PluginRepository(db).list_base_permissions(plugin_id=scope.plugin_id, version=scope.plugin_version)
        if ACTION_CAPABILITIES[operation.action] not in permissions:
            raise MeetingPluginDenied("operation permission was revoked")
        intent = db.get(AssistantActionIntentRecord, operation.intent_id)
        if intent.revoked_at is not None or _aware(intent.expires_at) <= self.actions.now():
            raise MeetingPluginDenied("operation intent expired or was revoked")
        if scope.action in {"meeting.execute", "meeting.input"}:
            self.require_capability_grant(db, scope)
        return scope

    def require_capability_grant(self, db, scope):
        from app.persistence.models import PluginCapabilityGrantRecord
        from app.plugins.host_actions import capability_grant_scope
        grant = db.get(PluginCapabilityGrantRecord, scope.capability_grant_id) if scope.capability_grant_id else None
        if (grant is None or grant.status != "active" or grant.revoked_at is not None
                or _aware(grant.expires_at) <= self.actions.now()
                or (grant.plugin_id, grant.plugin_version, grant.media_session_id, grant.capability, grant.effect) != (
                    scope.plugin_id, scope.plugin_version, scope.media_session_id, ACTION_CAPABILITIES[scope.action],
                    "external_write" if scope.action == "meeting.execute" else "local_write")
                or grant.scope_json != capability_grant_scope(scope)):
            raise MeetingPluginDenied("confirmed capability grant is unavailable")
        return grant

    def _cancel(self, db, operation, scope, arguments):
        from app.api.assistant import _external_effects
        repo = AssistantRepository(db)
        execution = MeetingPluginReadService(db).require_execution(scope.legacy_session_id, arguments["execution_id"])
        if is_terminal_execution_status(execution.profile, execution.status):
            raise MeetingPluginDenied("execution is already terminal")
        effects = _external_effects(repo, execution.id)
        repo.transition_execution(execution.id, expected_version=arguments["expected_state_version"],
            target_status="cancelled", event_type="execution.cancelled", summary="Execution cancelled by the user",
            result={**(execution.result_json or {}), "cancelled": True,
                "confirmed_external_side_effects": effects.confirmed, "unknown_external_side_effects": effects.unknown,
                "existing_external_actions_remain": effects.existing_actions_remain})
        operation.execution_id = execution.id
        operation.status = "completed"

    def _mark(self, db, operation, scope, arguments):
        from app.api.meeting_state import MarkEvidenceRef, _evidence_records, _manual_mark_message, _projection_segment
        repo = MeetingStateRepository(db)
        if arguments["operation"] != "create":
            repo.update_mark_status(arguments["mark_id"], "accepted" if arguments["operation"] == "accept" else "dismissed")
        else:
            evidence = tuple(MarkEvidenceRef.model_validate(item) for item in arguments["evidence"])
            records = _evidence_records(db, session_id=scope.legacy_session_id, evidence=evidence)
            identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, f"meeting-mark:{operation.id}"))
            now = utc_now()
            messages = tuple(caption_evidence_message(_projection_segment(row)) for row in records)
            manual = _manual_mark_message(mark_id=identifier, session_id=scope.legacy_session_id,
                actor_id=scope.actor_id, title=arguments["title"], note=arguments["note"], created_at=now)
            head = repo.get_head(scope.legacy_session_id)
            repo.create_mark(session_id=scope.legacy_session_id, mark_id=identifier, origin="manual",
                kind=arguments["kind"], title=arguments["title"], note=arguments["note"],
                source_segment_ids=tuple(e.segment_id for e in evidence),
                source_segment_revisions={e.segment_id: e.revision for e in evidence},
                evidence_messages=(*messages, manual), source_state_version=head.version if head else 0,
                audio_start_ms=min((r.audio_start_ms for r in records if r.audio_start_ms is not None), default=None),
                audio_end_ms=max((r.audio_end_ms for r in records if r.audio_end_ms is not None), default=None),
                status="accepted", created_at=now)
        operation.status = "completed"
