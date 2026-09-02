"""Finite Host action registry and trusted UI channel. No model/network calls."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from threading import RLock

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.assistant.plugin_contracts import (
    MeetingAskInput, MeetingCancelInput, MeetingExecuteInput, MeetingInputInput, MeetingMarkInput,
)
from app.assistant.plugin_policy import LIVE_SESSION_STATUSES
from app.assistant.plugin_repository import (
    MEETING_PLUGIN_ID, MeetingPluginDenied, MeetingPluginRepository, canonical_hash,
)
from app.assistant.plugin_service import MeetingPluginReadService
from app.persistence.database import Database
from app.persistence.models import (
    ActionGrantRecord, MeetingPluginSessionRecord, PluginInstallationRecord,
    PluginPackageRecord, PluginUIViewRecord, SessionRecord, utc_now,
)
from app.plugins.host_action_contracts import (
    HostActionConfirmInput, HostActionIntent, HostActionPrepareInput, HostActionScope,
)
from app.plugins.repository import PluginRepository
from app.settings import Settings


ACTION_CAPABILITIES = {
    "meeting.analysis.activate": "meeting.state.query",
    "meeting.analysis.deactivate": "meeting.state.query",
    "meeting.ask": "meeting.turn.submit",
    "meeting.execute": "meeting.execution.submit",
    "meeting.mark.create": "meeting.mark.write",
    "meeting.mark.accept": "meeting.mark.write",
    "meeting.mark.dismiss": "meeting.mark.write",
    "meeting.input": "meeting.execution.input",
    "meeting.cancel": "meeting.execution.cancel",
}
ACTION_MODELS: dict[str, type[BaseModel]] = {
    "meeting.ask": MeetingAskInput, "meeting.execute": MeetingExecuteInput,
    "meeting.input": MeetingInputInput, "meeting.cancel": MeetingCancelInput,
    **{f"meeting.mark.{verb}": MeetingMarkInput for verb in ("create", "accept", "dismiss")},
}


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def begin_write(db: Session) -> None:
    connection = db.connection()
    if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def normalize_arguments(action: str, arguments: dict[str, object]) -> dict[str, object]:
    if action not in ACTION_CAPABILITIES or {"intent_token", "request_id"} & arguments.keys():
        raise MeetingPluginDenied("invalid Host action arguments")
    if action.startswith("meeting.analysis."):
        if arguments:
            raise MeetingPluginDenied("analysis actions do not accept arguments")
        return {}
    values = dict(arguments)
    if action.startswith("meeting.mark."):
        operation = action.rsplit(".", 1)[1]
        if values.get("operation", operation) != operation:
            raise MeetingPluginDenied("mark action and operation differ")
        values["operation"] = operation
    model = ACTION_MODELS[action].model_validate({
        **values, "request_id": "validation-only", "intent_token": "x" * 48,
    })
    result = model.model_dump(mode="json", exclude={"request_id", "intent_token"})
    if "message" in result:
        result["message"] = " ".join(result["message"].split())
    if "input" in result:
        result["input"] = " ".join(result["input"].split())
    return result


@dataclass(frozen=True)
class TrustedUIContext:
    nonce_hash: str
    origin: str | None
    expires_at: dt.datetime
    actor_id: str = "local-user"


@dataclass
class _Preview:
    context: TrustedUIContext
    media_id: str
    request: HostActionPrepareInput
    scope: HostActionScope
    arguments: dict[str, object]
    preview_hash: str
    expires_at: dt.datetime
    response: dict[str, object] | None = None


class HostActions:
    def __init__(self, database: Database, settings: Settings, *, now=None):
        self.database = database
        self.settings = settings
        self.now = now or utc_now
        self.accepting = True  # The app composition closes this until startup completes.
        self.runtime_check = None
        self._contexts: dict[str, TrustedUIContext] = {}
        self._previews: dict[str, _Preview] = {}
        self._lock = RLock()

    def _cleanup(self):
        now = self.now()
        self._contexts = {key: value for key, value in self._contexts.items() if value.expires_at > now}
        self._previews = {key: value for key, value in self._previews.items() if value.expires_at > now}

    def dispatch_metadata(self, preview_id):
        with self._lock:
            preview = self._previews.get(preview_id)
            if preview is None:
                raise MeetingPluginDenied("preview expired")
            return preview.scope, preview.request.view_version

    def remember_admission(self, preview_id, result):
        with self._lock:
            preview = self._previews.get(preview_id)
            if preview is not None and result.get("status") == "accepted":
                preview.response = dict(result)

    def issue_ui_nonce(self, origin: str | None) -> str:
        # No-Origin authorization is enforced at the HTTP boundary before this
        # trusted method is called. It is never exposed as a plugin capability.
        if origin is not None and origin not in self.settings.cors_origin_list:
            raise MeetingPluginDenied("untrusted UI origin")
        with self._lock:
            self._cleanup()
            if len(self._contexts) >= self.settings.assistant_ui_context_limit:
                raise MeetingPluginDenied("too many UI contexts")
            nonce = secrets.token_urlsafe(48)
            key = token_hash(nonce)
            self._contexts[key] = TrustedUIContext(key, origin,
                self.now() + dt.timedelta(seconds=self.settings.assistant_ui_nonce_ttl_seconds))
            return nonce

    def require_ui_context(self, nonce: str, origin: str | None) -> TrustedUIContext:
        with self._lock:
            self._cleanup()
            context = self._contexts.get(token_hash(nonce))
            if context is None or context.origin != origin:
                raise MeetingPluginDenied("trusted UI context is missing or expired")
            return context

    def _require_context(self, context: TrustedUIContext):
        self._cleanup()
        if self._contexts.get(context.nonce_hash) != context:
            raise MeetingPluginDenied("trusted UI context is missing or expired")

    def build_scope(self, db: Session, *, media_id: str, request: HostActionPrepareInput,
                    actor_id: str, history_cancel: bool = False):
        if history_cancel and request.action != "meeting.cancel":
            raise MeetingPluginDenied("history only permits cancellation")
        if not self.accepting or (not history_cancel and not (self.settings.assistant_enabled and self.settings.plugin_framework_enabled)):
            raise MeetingPluginDenied("meeting actions are unavailable")
        service = MeetingPluginReadService(db)
        session_id = service.resolve(media_id)
        control = db.get(MeetingPluginSessionRecord, session_id, populate_existing=True)
        if history_cancel:
            version = control.plugin_version if control else "0.0.0"
        else:
            installation = db.scalar(select(PluginInstallationRecord).where(
                PluginInstallationRecord.plugin_id == MEETING_PLUGIN_ID,
                PluginInstallationRecord.status == "enabled",
            ))
            package = db.get(PluginPackageRecord, installation.preferred_package_id) if installation else None
            if package is None:
                raise MeetingPluginDenied("meeting plugin is unavailable")
            version = package.version
            if self.runtime_check is not None:
                self.runtime_check(db, media_id, request.action, version)
            service.require_scope(media_id, MEETING_PLUGIN_ID, version)
            permissions = PluginRepository(db).list_base_permissions(plugin_id=MEETING_PLUGIN_ID, version=version)
            if ACTION_CAPABILITIES[request.action] not in permissions:
                raise MeetingPluginDenied("meeting action permission is unavailable")
            control = MeetingPluginRepository(db).ensure_session(
                legacy_session_id=session_id, media_session_id=media_id, plugin_version=version,
            )
        if request.plugin_version is not None and request.plugin_version != version:
            raise MeetingPluginDenied("plugin version changed")
        if request.view_version is not None:
            view = db.scalar(select(PluginUIViewRecord).where(
                PluginUIViewRecord.plugin_id == MEETING_PLUGIN_ID,
                PluginUIViewRecord.plugin_version == version,
                PluginUIViewRecord.media_session_id == media_id,
            ))
            if view is None or view.view_version != request.view_version:
                raise MeetingPluginDenied("plugin view changed")
        arguments = normalize_arguments(request.action, request.arguments)
        resources: dict[str, object] = {}
        candidates = []
        for mark_id in arguments.get("mark_ids", ()):
            mark = service.require_mark(session_id, mark_id)
            resources[mark_id] = {"status": mark.status, "revision": mark.source_state_version,
                "evidence": mark.source_segment_revisions_json}
        analysis_epoch = None
        grant_id = None
        if request.action.startswith("meeting.analysis."):
            if request.action == "meeting.analysis.activate" and db.get(SessionRecord, session_id).status not in LIVE_SESSION_STATUSES:
                raise MeetingPluginDenied("analysis requires a live Session")
            analysis_epoch = control.analysis_epoch
        elif request.action.startswith("meeting.mark."):
            from app.api.meeting_state import MarkEvidenceRef, _evidence_records
            if arguments["evidence"]:
                _evidence_records(db, session_id=session_id,
                    evidence=tuple(MarkEvidenceRef.model_validate(e) for e in arguments["evidence"]))
            if arguments["mark_id"]:
                mark = service.require_mark(session_id, arguments["mark_id"])
                if mark.source_state_version != arguments["expected_state_version"]:
                    raise MeetingPluginDenied("mark version changed")
                if request.action == "meeting.mark.accept" and {
                    item["segment_id"]: item["revision"] for item in arguments["evidence"]
                } != mark.source_segment_revisions_json:
                    raise MeetingPluginDenied("mark evidence differs from the proposed revisions")
                resources[mark.id] = {"status": mark.status, "evidence": mark.source_segment_revisions_json}
        elif request.action in {"meeting.input", "meeting.cancel"}:
            execution = service.require_execution(session_id, arguments["execution_id"])
            if execution.state_version != arguments["expected_state_version"]:
                raise MeetingPluginDenied("execution version changed")
            resources[execution.id] = {"version": execution.state_version, "status": execution.status}
            grant_id = execution.grant_id
            if request.action == "meeting.input":
                if execution.status != "needs_input":
                    raise MeetingPluginDenied("execution is not waiting for input")
                if grant_id:
                    grant = db.get(ActionGrantRecord, grant_id)
                    if grant is None or grant.status != "active" or _aware(grant.expires_at) <= self.now():
                        raise MeetingPluginDenied("external-write authority must be renewed")
        team_id = None
        if request.action == "meeting.execute":
            if self.settings.task_system_provider == "disabled":
                raise MeetingPluginDenied("task system is unavailable")
            team_id = self.settings.linear_team_id or (
                "local-demo-team" if self.settings.task_system_provider == "fake" else None
            )
            if not team_id:
                raise MeetingPluginDenied("task system team is unavailable")
            for candidate_id in arguments["candidate_ids"]:
                candidate = service.require_candidate(session_id, candidate_id)
                if candidate.content_status != "active" or not candidate.evidence_revisions_current:
                    raise MeetingPluginDenied("candidate changed or its evidence is stale")
                candidates.append(candidate)
                resources[candidate_id] = candidate.model_dump(mode="json")
        scope = HostActionScope(
            plugin_id=MEETING_PLUGIN_ID, plugin_version=version, media_session_id=media_id,
            legacy_session_id=session_id, actor_id=actor_id,
            authority_epoch=control.authority_epoch if control else 0, analysis_epoch=analysis_epoch,
            action=request.action, payload_hash=canonical_hash({"request_id": request.request_id, "arguments": arguments}),
            candidate_revisions={c.candidate_id: c.current_revision for c in candidates},
            snapshot_id=canonical_hash(resources), team_id=team_id, max_side_effects=len(candidates),
            source="host_history" if history_cancel else "plugin", action_grant_id=grant_id,
        )
        return scope, arguments, candidates

    def prepare(self, context: TrustedUIContext, media_id: str, request: HostActionPrepareInput,
                *, history_cancel: bool = False) -> dict[str, object]:
        with self._lock:
            self._require_context(context)
            if len(self._previews) >= self.settings.assistant_ui_context_limit * 4:
                raise MeetingPluginDenied("too many pending previews")
            with self.database.session() as db:
                begin_write(db)
                scope, arguments, candidates = self.build_scope(db, media_id=media_id,
                    request=request, actor_id=context.actor_id, history_cancel=history_cancel)
                db.commit()
            expires_at = min(context.expires_at, self.now() + dt.timedelta(
                seconds=min(900, self.settings.assistant_grant_max_ttl_seconds)))
            preview_id = secrets.token_urlsafe(32)
            public = {
                "preview_id": preview_id, "action": scope.action,
                "effect": "external_write" if scope.action == "meeting.execute" else "local_write",
                "confirmation_required": scope.action == "meeting.execute",
                "team_id": scope.team_id, "max_side_effects": scope.max_side_effects,
                "candidates": [c.model_dump(mode="json") for c in candidates],
                "expires_at": expires_at.isoformat(),
            }
            digest = canonical_hash({"scope": scope.model_dump(mode="json"), "preview": public})
            self._previews[preview_id] = _Preview(context, media_id,
                HostActionPrepareInput.model_validate(request.model_dump(mode="json")),
                scope, arguments, digest, expires_at)
            return {**public, "preview_hash": digest}

    def confirm(self, context: TrustedUIContext, media_id: str,
                value: HostActionConfirmInput) -> dict[str, object]:
        with self._lock:
            self._require_context(context)
            preview = self._previews.get(value.preview_id)
            if preview is None or preview.media_id != media_id or preview.context != context:
                raise MeetingPluginDenied("preview is missing or out of scope")
            if not hmac.compare_digest(preview.preview_hash, value.preview_hash):
                raise MeetingPluginDenied("preview changed")
            if not value.confirmed:
                self._previews.pop(value.preview_id)
                return {"status": "cancelled"}
            if preview.response is not None:
                return copy.deepcopy(preview.response)
            with self.database.session() as db:
                begin_write(db)
                scope, arguments, _ = self.build_scope(db, media_id=media_id,
                    request=preview.request, actor_id=context.actor_id,
                    history_cancel=preview.scope.source == "host_history")
                if scope != preview.scope:
                    raise MeetingPluginDenied("preview scope changed; prepare again")
                token = secrets.token_urlsafe(48)
                if scope.action in {"meeting.execute", "meeting.input"}:
                    grant = PluginRepository(db).create_capability_grant(
                        plugin_id=scope.plugin_id, version=scope.plugin_version,
                        media_session_id=media_id, capability=ACTION_CAPABILITIES[scope.action],
                        effect="external_write" if scope.action == "meeting.execute" else "local_write",
                        scope=capability_grant_scope(scope), expires_at=preview.expires_at,
                    )
                    scope = scope.model_copy(update={"capability_grant_id": grant.id})
                repo = MeetingPluginRepository(db)
                repo.create_intent(HostActionIntent(token_hash=token_hash(token), scope=scope,
                    created_at=self.now(), expires_at=preview.expires_at))
                if scope.action.startswith("meeting.analysis."):
                    repo.consume_intent(token_hash=token_hash(token), expected_scope=scope, now=self.now())
                    row = db.get(MeetingPluginSessionRecord, scope.legacy_session_id)
                    if scope.action == "meeting.analysis.activate":
                        if row.analysis_state != "active":
                            row.analysis_epoch += 1
                            row.analysis_state = "active"
                            row.activated_by = context.actor_id
                            row.activated_at = self.now()
                            row.stopped_reason = None
                            row.terminal_frontier_json = None
                    else:
                        repo.stop_analysis(scope.legacy_session_id)
                    response = {"status": "applied", "action": scope.action}
                elif scope.source == "host_history":
                    from app.assistant.plugin_operations import MeetingPluginOperations
                    operation = MeetingPluginOperations(self).admit(
                        db, plugin_id=scope.plugin_id, plugin_version=scope.plugin_version,
                        media_session_id=media_id, action=scope.action,
                        command={**arguments, "request_id": preview.request.request_id, "intent_token": token},
                        from_host_history=True,
                    )
                    response = {"status": "accepted", "operation_id": operation.id, "action": scope.action}
                else:
                    response = {"status": "authorized", "action": scope.action,
                        "command": {**arguments, "request_id": preview.request.request_id, "intent_token": token}}
                db.commit()
            preview.response = copy.deepcopy(response)
            return response


def _aware(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


def capability_grant_scope(scope: HostActionScope) -> dict[str, object]:
    """Only the Host can derive the requested authority; an empty scope is unsafe."""
    return {"authority_epoch": scope.authority_epoch, "action": scope.action,
        "payload_hash": scope.payload_hash, "snapshot_id": scope.snapshot_id,
        "team_id": scope.team_id, "candidate_revisions": dict(scope.candidate_revisions),
        "max_side_effects": scope.max_side_effects, "action_grant_id": scope.action_grant_id}
