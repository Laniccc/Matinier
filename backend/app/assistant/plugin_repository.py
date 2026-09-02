"""Short caller-owned transactions for meeting-plugin control records.

No method commits, invokes a model or grants permission. Admission policy and
trusted UI verification are separate layers; callers must apply them first.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.persistence.models import (
    AssistantActionIntentRecord, MediaSessionRecord,
    MeetingPluginOperationRecord, MeetingPluginSessionRecord, utc_now,
)
from app.plugins.host_action_contracts import HostActionIntent, HostActionScope


MEETING_PLUGIN_ID = "com.matinier.meeting-assistant"


class MeetingPluginConflict(ValueError):
    pass


class MeetingPluginDenied(PermissionError):
    pass


def canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > 256 * 1024:
        raise ValueError("meeting payload exceeds byte limit")
    return hashlib.sha256(encoded).hexdigest()


class MeetingPluginRepository:
    def __init__(self, db: Session):
        self.db = db

    def _check_mapping(self, legacy_session_id: str, media_session_id: str):
        media = self.db.get(MediaSessionRecord, media_session_id)
        if media is None or media.legacy_session_id != legacy_session_id:
            raise MeetingPluginConflict("MediaSession does not match legacy Session")

    def ensure_session(self, *, legacy_session_id: str, media_session_id: str, plugin_version: str):
        self._check_mapping(legacy_session_id, media_session_id)
        if not plugin_version.strip() or len(plugin_version) > 64:
            raise ValueError("invalid plugin version")
        self.db.execute(insert(MeetingPluginSessionRecord).values(
            legacy_session_id=legacy_session_id, media_session_id=media_session_id,
            plugin_id=MEETING_PLUGIN_ID, plugin_version=plugin_version,
            analysis_state="inactive", analysis_epoch=0, authority_epoch=0,
        ).on_conflict_do_nothing())
        row = self.db.get(MeetingPluginSessionRecord, legacy_session_id, populate_existing=True)
        if row is None or (row.media_session_id, row.plugin_id, row.plugin_version) != (media_session_id, MEETING_PLUGIN_ID, plugin_version):
            raise MeetingPluginConflict("meeting session has another owner or version")
        return row

    def accept_operation(self, *, scope: HostActionScope, client_request_id: str, request_payload: dict[str, object], intent_id: str | None = None):
        self._check_mapping(scope.legacy_session_id, scope.media_session_id)
        if not client_request_id.strip() or len(client_request_id) > 255:
            raise ValueError("invalid client request ID")
        def reject_secrets(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"intent_token", "admin_token", "api_key"}:
                        raise ValueError("raw credentials must not be persisted")
                    reject_secrets(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    reject_secrets(child)
        reject_secrets(request_payload)
        request_hash = canonical_hash({"scope": scope.model_dump(mode="json"), "payload": request_payload})
        self.db.execute(insert(MeetingPluginOperationRecord).values(
            id=str(uuid.uuid4()), plugin_id=scope.plugin_id, plugin_version=scope.plugin_version,
            media_session_id=scope.media_session_id, legacy_session_id=scope.legacy_session_id,
            authority_epoch=scope.authority_epoch, intent_id=intent_id,
            client_request_id=client_request_id, action=scope.action, request_hash=request_hash,
            request_payload_json=request_payload, status="accepted",
        ).on_conflict_do_nothing(index_elements=["plugin_id", "plugin_version", "media_session_id", "client_request_id"]))
        row = self.db.scalar(select(MeetingPluginOperationRecord).where(
            MeetingPluginOperationRecord.plugin_id == scope.plugin_id,
            MeetingPluginOperationRecord.plugin_version == scope.plugin_version,
            MeetingPluginOperationRecord.media_session_id == scope.media_session_id,
            MeetingPluginOperationRecord.client_request_id == client_request_id,
        ))
        if row is None or row.request_hash != request_hash:
            raise MeetingPluginConflict("request ID already binds a different operation")
        return row

    def create_intent(self, intent: HostActionIntent):
        scope = intent.scope
        self._check_mapping(scope.legacy_session_id, scope.media_session_id)
        payload = scope.model_dump(mode="json")
        row = AssistantActionIntentRecord(
            id=str(uuid.uuid4()), token_hash=intent.token_hash,
            plugin_id=scope.plugin_id, plugin_version=scope.plugin_version,
            media_session_id=scope.media_session_id, legacy_session_id=scope.legacy_session_id,
            authority_epoch=scope.authority_epoch, scope_hash=canonical_hash(payload),
            scope_json=payload, status="active", created_at=intent.created_at,
            expires_at=intent.expires_at,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def consume_intent(self, *, token_hash: str, expected_scope: HostActionScope, now: dt.datetime | None = None):
        now = now or utc_now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("intent clock must be timezone aware")
        result = self.db.execute(update(AssistantActionIntentRecord).where(
            AssistantActionIntentRecord.token_hash == token_hash,
            AssistantActionIntentRecord.scope_hash == canonical_hash(expected_scope.model_dump(mode="json")),
            AssistantActionIntentRecord.status == "active",
            AssistantActionIntentRecord.revoked_at.is_(None),
            AssistantActionIntentRecord.consumed_at.is_(None),
            AssistantActionIntentRecord.created_at <= now,
            AssistantActionIntentRecord.expires_at > now,
        ).values(status="consumed", consumed_at=now).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise MeetingPluginDenied("intent is invalid, out of scope, expired or consumed")
        return self.db.scalar(select(AssistantActionIntentRecord).where(AssistantActionIntentRecord.token_hash == token_hash).execution_options(populate_existing=True))

    def stop_analysis(self, legacy_session_id: str, *, reason="user_stopped"):
        return self.db.execute(update(MeetingPluginSessionRecord).where(
            MeetingPluginSessionRecord.legacy_session_id == legacy_session_id,
        ).values(analysis_state="inactive", analysis_epoch=MeetingPluginSessionRecord.analysis_epoch + 1,
                 stopped_reason=reason, updated_at=utc_now()).execution_options(synchronize_session=False)).rowcount

    def revoke_authority(self, legacy_session_id: str, *, reason="plugin_disabled"):
        now = utc_now()
        self.db.execute(update(MeetingPluginSessionRecord).where(
            MeetingPluginSessionRecord.legacy_session_id == legacy_session_id,
        ).values(analysis_state="inactive", analysis_epoch=MeetingPluginSessionRecord.analysis_epoch + 1,
                 authority_epoch=MeetingPluginSessionRecord.authority_epoch + 1,
                 stopped_reason=reason, updated_at=now).execution_options(synchronize_session=False))
        self.db.execute(update(AssistantActionIntentRecord).where(
            AssistantActionIntentRecord.legacy_session_id == legacy_session_id,
            AssistantActionIntentRecord.plugin_id == MEETING_PLUGIN_ID,
            AssistantActionIntentRecord.status == "active",
        ).values(status="revoked", revoked_at=now).execution_options(synchronize_session=False))
