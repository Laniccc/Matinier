from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Base(DeclarativeBase):
    pass


class MeetingPluginSessionRecord(Base):
    __tablename__ = "meeting_plugin_sessions"
    __table_args__ = (
        CheckConstraint("analysis_epoch >= 0 AND authority_epoch >= 0", name="ck_meeting_plugin_session_epochs"),
        CheckConstraint("analysis_state IN ('inactive','active','draining','completed')", name="ck_meeting_plugin_analysis_state"),
        Index("ix_meeting_plugin_sessions_analysis", "analysis_state", "plugin_id"),
    )
    legacy_session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True)
    media_session_id: Mapped[str] = mapped_column(String(36), ForeignKey("media_sessions.id", ondelete="CASCADE"), nullable=False, unique=True)
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plugin_version: Mapped[str] = mapped_column(String(64), nullable=False)
    analysis_state: Mapped[str] = mapped_column(String(16), nullable=False, default="inactive", server_default="inactive")
    analysis_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    authority_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    activated_by: Mapped[str | None] = mapped_column(String(255))
    activated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_reason: Mapped[str | None] = mapped_column(String(128))
    terminal_frontier_json: Mapped[dict[str, int] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class AssistantActionIntentRecord(Base):
    __tablename__ = "assistant_action_intents"
    __table_args__ = (
        CheckConstraint("status IN ('active','consumed','revoked')", name="ck_assistant_action_intent_status"),
        Index("ix_assistant_action_intents_scope", "plugin_id", "media_session_id", "status"),
        Index("ix_assistant_action_intents_expiry", "expires_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plugin_version: Mapped[str] = mapped_column(String(64), nullable=False)
    media_session_id: Mapped[str] = mapped_column(String(36), ForeignKey("media_sessions.id", ondelete="CASCADE"), nullable=False)
    legacy_session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    authority_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class MeetingPluginOperationRecord(Base):
    __tablename__ = "meeting_plugin_operations"
    __table_args__ = (
        UniqueConstraint("plugin_id", "plugin_version", "media_session_id", "client_request_id", name="uq_meeting_plugin_operations_request"),
        CheckConstraint("authority_epoch >= 0", name="ck_meeting_plugin_operation_epoch"),
        CheckConstraint("status IN ('accepted','running','completed','failed','cancelled')", name="ck_meeting_plugin_operation_status"),
        Index("ix_meeting_plugin_operations_status", "status", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plugin_version: Mapped[str] = mapped_column(String(64), nullable=False)
    media_session_id: Mapped[str] = mapped_column(String(36), ForeignKey("media_sessions.id", ondelete="CASCADE"), nullable=False)
    legacy_session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    authority_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    intent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("assistant_action_intents.id", ondelete="SET NULL"))
    execution_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("assistant_executions.id", ondelete="SET NULL"))
    client_request_id: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_payload_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="accepted", server_default="accepted")
    error_code: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class RoomRecord(Base):
    __tablename__ = "managed_rooms"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    room_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
    closed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class SessionRecord(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    room_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("managed_rooms.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    room_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    language: Mapped[str] = mapped_column(String(32), nullable=False)
    target_language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    asr_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    asr_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    translation_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="disabled",
    )
    translation_provider: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    translation_model: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    translation_error_code: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    translation_error_message: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
    )
    translation_ended_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    source_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="starting",
    )
    source_ended_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    cleanup_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="not_started",
    )
    cleanup_detail: Mapped[str | None] = mapped_column(
        String(2000),
        nullable=True,
    )
    final_result_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_partial_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_final_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    provider_error_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sent_audio_chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sent_audio_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class SegmentRecord(Base):
    __tablename__ = "segments"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "segment_id",
            name="uq_segments_session_segment",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    segment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    track_id: Mapped[str] = mapped_column(String(255), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    display_text: Mapped[str] = mapped_column(Text, nullable=False)
    audio_start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    received_at_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    finalized_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class TranslationSegmentRecord(Base):
    __tablename__ = "translation_segments"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "target_language",
            "segment_id",
            name="uq_translation_segments_session_language_segment",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    segment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_language: Mapped[str] = mapped_column(String(32), nullable=False)
    target_language: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    audio_start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_segment_ids: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    received_at_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    finalized_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class MediaSessionRecord(Base):
    __tablename__ = "media_sessions"
    __table_args__ = (
        Index("ix_media_sessions_status_updated", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    legacy_session_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("sessions.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
        index=True,
    )
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_scope: Mapped[str | None] = mapped_column(String(256), nullable=True)
    next_sequence: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class MediaEventRecord(Base):
    __tablename__ = "media_events"
    __table_args__ = (
        UniqueConstraint(
            "media_session_id",
            "sequence",
            name="uq_media_events_session_sequence",
        ),
        UniqueConstraint(
            "media_session_id",
            "source",
            "logical_id",
            "revision",
            name="uq_media_events_source_revision",
        ),
        Index("ix_media_events_session_cursor", "media_session_id", "sequence"),
        Index("ix_media_events_type_created", "event_type", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    media_session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("media_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    event_type: Mapped[str] = mapped_column(String(96), nullable=False)
    media_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    logical_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finality: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(96), nullable=False)
    payload_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class MediaBridgeOffsetRecord(Base):
    __tablename__ = "media_bridge_offsets"
    __table_args__ = (
        UniqueConstraint(
            "media_session_id",
            "source_table",
            "source_type",
            "source_logical_id",
            name="uq_media_bridge_offsets_source",
        ),
        Index(
            "ix_media_bridge_offsets_session_source",
            "media_session_id",
            "source_table",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    media_session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("media_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_table: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(96), nullable=False)
    source_logical_id: Mapped[str] = mapped_column(String(256), nullable=False)
    processed_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    processed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class MediaConsumerCursorRecord(Base):
    __tablename__ = "media_consumer_cursors"
    __table_args__ = (
        UniqueConstraint(
            "media_session_id",
            "consumer_id",
            name="uq_media_consumer_cursors_session_consumer",
        ),
        Index(
            "ix_media_consumer_cursors_consumer",
            "consumer_id",
            "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    media_session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("media_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    consumer_id: Mapped[str] = mapped_column(String(192), nullable=False)
    last_delivered_sequence: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    last_acknowledged_sequence: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class PluginPublisherRecord(Base):
    __tablename__ = "plugin_publishers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    key_fingerprint: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        unique=True,
        index=True,
    )
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    trusted_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class PluginPackageRecord(Base):
    __tablename__ = "plugin_packages"
    __table_args__ = (
        UniqueConstraint("plugin_id", "version", name="uq_plugin_packages_id_version"),
        UniqueConstraint("content_digest", name="uq_plugin_packages_content_digest"),
        Index("ix_plugin_packages_plugin_installed", "plugin_id", "installed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    content_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    image_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    runtime_image_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    signature_status: Mapped[str] = mapped_column(String(32), nullable=False)
    package_path: Mapped[str] = mapped_column(Text, nullable=False)
    publisher_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("plugin_publishers.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    manifest_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    installed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class PluginInstallationRecord(Base):
    __tablename__ = "plugin_installations"
    __table_args__ = (Index("ix_plugin_installations_status", "status", "updated_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    preferred_package_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("plugin_packages.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class PluginStagingTicketRecord(Base):
    __tablename__ = "plugin_staging_tickets"
    __table_args__ = (
        Index("ix_plugin_staging_tickets_expiry", "expires_at", "consumed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    content_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    staged_path: Mapped[str] = mapped_column(Text, nullable=False)
    permission_request_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    signature_status: Mapped[str] = mapped_column(String(32), nullable=False)
    publisher_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    publisher_fingerprint: Mapped[str | None] = mapped_column(String(80), nullable=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class PluginPermissionRecord(Base):
    __tablename__ = "plugin_permissions"
    __table_args__ = (
        UniqueConstraint(
            "package_id", "permission_name", name="uq_plugin_permissions_package_name"
        ),
        Index("ix_plugin_permissions_plugin_version", "plugin_id", "plugin_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plugin_version: Mapped[str] = mapped_column(String(64), nullable=False)
    package_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plugin_packages.id", ondelete="CASCADE"), nullable=False
    )
    permission_name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class PluginRuntimeHealthRecord(Base):
    __tablename__ = "plugin_runtime_health"
    __table_args__ = (
        UniqueConstraint("package_id", name="uq_plugin_runtime_health_package"),
        Index("ix_plugin_runtime_health_status", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plugin_version: Mapped[str] = mapped_column(String(64), nullable=False)
    package_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plugin_packages.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    crash_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_started_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_heartbeat_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    quarantine_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class PluginSessionBindingRecord(Base):
    __tablename__ = "plugin_session_bindings"
    __table_args__ = (
        UniqueConstraint(
            "package_id", "media_session_id", name="uq_plugin_bindings_package_session"
        ),
        UniqueConstraint("session_scope", name="uq_plugin_bindings_session_scope"),
        Index("ix_plugin_bindings_state", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plugin_version: Mapped[str] = mapped_column(String(64), nullable=False)
    package_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plugin_packages.id", ondelete="RESTRICT"), nullable=False
    )
    media_session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("media_sessions.id", ondelete="CASCADE"), nullable=False
    )
    session_scope: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_delivered_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_acknowledged_sequence: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    opened_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    closed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class PluginStateItemRecord(Base):
    __tablename__ = "plugin_state_items"
    __table_args__ = (
        UniqueConstraint(
            "package_id", "namespace", "item_key", name="uq_plugin_state_package_key"
        ),
        Index("ix_plugin_state_namespace", "plugin_id", "namespace"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plugin_version: Mapped[str] = mapped_column(String(64), nullable=False)
    package_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plugin_packages.id", ondelete="CASCADE"), nullable=False
    )
    namespace: Mapped[str] = mapped_column(String(192), nullable=False)
    item_key: Mapped[str] = mapped_column(String(192), nullable=False)
    value_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class PluginUIViewRecord(Base):
    __tablename__ = "plugin_ui_views"
    __table_args__ = (
        UniqueConstraint(
            "package_id",
            "media_session_id",
            "surface",
            "view_id",
            name="uq_plugin_ui_views_identity",
        ),
        Index("ix_plugin_ui_views_session_surface", "media_session_id", "surface"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plugin_version: Mapped[str] = mapped_column(String(64), nullable=False)
    package_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plugin_packages.id", ondelete="CASCADE"), nullable=False
    )
    media_session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("media_sessions.id", ondelete="CASCADE"), nullable=False
    )
    surface: Mapped[str] = mapped_column(String(32), nullable=False)
    view_id: Mapped[str] = mapped_column(String(128), nullable=False)
    view_version: Mapped[int] = mapped_column(Integer, nullable=False)
    view_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class PluginCapabilityGrantRecord(Base):
    __tablename__ = "plugin_capability_grants"
    __table_args__ = (
        Index("ix_plugin_grants_expiry", "status", "expires_at"),
        Index("ix_plugin_grants_plugin_session", "plugin_id", "media_session_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plugin_version: Mapped[str] = mapped_column(String(64), nullable=False)
    package_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plugin_packages.id", ondelete="CASCADE"), nullable=False
    )
    media_session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("media_sessions.id", ondelete="CASCADE"), nullable=True
    )
    capability: Mapped[str] = mapped_column(String(160), nullable=False)
    effect: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class PluginCapabilityInvocationRecord(Base):
    __tablename__ = "plugin_capability_invocations"
    __table_args__ = (
        UniqueConstraint(
            "plugin_id", "idempotency_key", name="uq_plugin_invocations_idempotency"
        ),
        Index("ix_plugin_invocations_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plugin_version: Mapped[str] = mapped_column(String(64), nullable=False)
    package_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plugin_packages.id", ondelete="RESTRICT"), nullable=False
    )
    media_session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("media_sessions.id", ondelete="SET NULL"), nullable=True
    )
    capability: Mapped[str] = mapped_column(String(160), nullable=False)
    effect: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    outcome_unknown: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PluginAuditEventRecord(Base):
    __tablename__ = "plugin_audit_events"
    __table_args__ = (
        Index("ix_plugin_audit_time", "plugin_id", "created_at"),
        Index("ix_plugin_audit_event_type", "event_type", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plugin_version: Mapped[str] = mapped_column(String(64), nullable=False)
    package_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plugin_packages.id", ondelete="RESTRICT"), nullable=False
    )
    media_session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("media_sessions.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ProcessedScriptRecord(Base):
    __tablename__ = "processed_scripts"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "version",
            name="uq_processed_scripts_session_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_segment_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    markdown_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class ResultPackageRecord(Base):
    __tablename__ = "result_packages"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "version",
            name="uq_result_packages_session_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_name: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    source_revision_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    frozen_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    superseded_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class PackageDocumentRecord(Base):
    __tablename__ = "package_documents"
    __table_args__ = (
        UniqueConstraint(
            "package_id",
            "document_kind",
            "language",
            name="uq_package_documents_identity",
        ),
        Index(
            "uq_package_documents_identity_no_language",
            "package_id",
            "document_kind",
            unique=True,
            sqlite_where=text("language IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    package_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("result_packages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    content_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class PluginDocumentRecord(Base):
    __tablename__ = "plugin_documents"
    __table_args__ = (
        UniqueConstraint(
            "plugin_id",
            "media_session_id",
            "identity_key",
            "document_version",
            name="uq_plugin_documents_identity_version",
        ),
        Index(
            "ix_plugin_documents_session_plugin_created",
            "media_session_id",
            "plugin_id",
            "created_at",
        ),
        Index("ix_plugin_documents_source_package", "source_package_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plugin_version: Mapped[str] = mapped_column(String(64), nullable=False)
    plugin_package_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("plugin_packages.id", ondelete="SET NULL"),
        nullable=True,
    )
    media_session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("media_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_package_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("result_packages.id", ondelete="RESTRICT"),
        nullable=False,
    )
    identity_key: Mapped[str] = mapped_column(String(192), nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_name: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    language: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    completeness: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="published",
        server_default="published",
    )
    source_package_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_package_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    evidence_refs_json: Mapped[list[dict[str, object]]] = mapped_column(
        JSON,
        nullable=False,
    )
    markdown_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class TranscriptRevisionRecord(Base):
    __tablename__ = "transcript_revisions"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "language",
            "version",
            name="uq_transcript_revisions_session_language_version",
        ),
        Index(
            "uq_transcript_revisions_current_approved",
            "session_id",
            "language",
            unique=True,
            sqlite_where=text("status = 'approved'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_revision_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("transcript_revisions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    base_package_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("result_packages.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    language: Mapped[str] = mapped_column(String(32), nullable=False)
    content_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    change_summary: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    approved_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class DerivedArtifactRecord(Base):
    __tablename__ = "derived_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "package_id",
            "identity_key",
            "artifact_version",
            name="uq_derived_artifacts_identity_version",
        ),
        Index(
            "uq_derived_artifacts_current_approved",
            "package_id",
            "identity_key",
            unique=True,
            sqlite_where=text("status = 'approved'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    package_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("result_packages.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    package_version: Mapped[int] = mapped_column(Integer, nullable=False)
    package_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    identity_key: Mapped[str] = mapped_column(String(256), nullable=False)
    artifact_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    workflow_version: Mapped[str] = mapped_column(String(32), nullable=False)
    options_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    content_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    evidence_json: Mapped[list[dict[str, object]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    parent_artifact_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("derived_artifacts.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    created_by: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    approved_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class ProcessingJobRecord(Base):
    __tablename__ = "processing_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    package_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("result_packages.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    target_artifact_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("derived_artifacts.id", ondelete="RESTRICT"),
        nullable=True,
    )
    result_artifact_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("derived_artifacts.id", ondelete="SET NULL"),
        nullable=True,
    )
    artifact_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    options_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    ended_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class MeetingProjectionOffsetRecord(Base):
    __tablename__ = "meeting_projection_offsets"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "segment_id",
            name="uq_meeting_projection_offsets_session_segment",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    segment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    processed_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    processed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class MeetingStateHeadRecord(Base):
    __tablename__ = "meeting_state_heads"

    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    state_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_frontier_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    latest_final_updated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    projected_through: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    lag_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_segment_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    last_success_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_error_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_error_code: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class MeetingMarkRecord(Base):
    __tablename__ = "meeting_marks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_segment_ids_json: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    source_segment_revisions_json: Mapped[dict[str, int]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    evidence_messages_json: Mapped[list[dict[str, object]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    audio_start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class ActionCandidateRecord(Base):
    __tablename__ = "action_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lineage_root_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        index=True,
    )
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
    )
    execution_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
    )
    superseded_by_candidate_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("action_candidates.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    derived_from_candidate_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("action_candidates.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    derived_from_revision: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class ActionCandidateRevisionRecord(Base):
    __tablename__ = "action_candidate_revisions"
    __table_args__ = (
        UniqueConstraint(
            "candidate_id",
            "revision",
            name="uq_action_candidate_revisions_candidate_revision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("action_candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    readiness: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    content_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    change_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    parent_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    change_summary: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class IdentityBindingRecord(Base):
    __tablename__ = "identity_bindings"
    __table_args__ = (
        UniqueConstraint(
            "actor_id",
            "linear_team_id",
            "normalized_mention",
            "status",
            name="uq_identity_bindings_actor_team_mention_status",
        ),
        Index(
            "ix_identity_bindings_lookup",
            "actor_id",
            "linear_team_id",
            "normalized_mention",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    linear_team_id: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_mention: Mapped[str] = mapped_column(String(255), nullable=False)
    linear_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    confirmed_by_actor_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    last_verified_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class AssistantContextSnapshotRecord(Base):
    __tablename__ = "assistant_context_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    meeting_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state_slice_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    source_frontier_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    evidence_refs_json: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    evidence_messages_json: Mapped[list[dict[str, object]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    relevant_context_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class ActionGrantRecord(Base):
    __tablename__ = "action_grants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    capabilities_json: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    resource_scope_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    candidate_ids_json: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    linear_team_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    max_side_effects: Mapped[int] = mapped_column(Integer, nullable=False)
    used_side_effects: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    unresolved_identity_policy: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="placeholder",
    )
    expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class AssistantExecutionRecord(Base):
    __tablename__ = "assistant_executions"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "client_request_id",
            name="uq_assistant_executions_session_client_request",
        ),
        Index(
            "ix_assistant_executions_session_status_profile",
            "session_id",
            "status",
            "profile",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    profile: Mapped[str] = mapped_column(String(32), nullable=False)
    root_execution_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("assistant_executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_execution_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("assistant_executions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    snapshot_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("assistant_context_snapshots.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    grant_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("action_grants.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    client_request_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    step_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    budget_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    result_json: Mapped[dict[str, object] | None] = mapped_column(
        JSON,
        nullable=True,
    )
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class AssistantActionApprovalRecord(Base):
    __tablename__ = "assistant_action_approvals"
    __table_args__ = (
        Index(
            "uq_assistant_action_approvals_pending_execution",
            "execution_id",
            unique=True,
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_assistant_action_approvals_session_status",
            "session_id",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("assistant_executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    capability: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    logical_action_key: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    resource_scope_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    arguments_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    evidence_refs_json: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    display_summary_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    grant_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("action_grants.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class AssistantPackageBindingRecord(Base):
    __tablename__ = "assistant_package_bindings"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            name="uq_assistant_package_bindings_execution",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("assistant_executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("assistant_context_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    package_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("result_packages.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    package_version: Mapped[int] = mapped_column(Integer, nullable=False)
    package_content_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    segment_item_mapping_json: Mapped[dict[str, list[str]]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class AssistantStepRecord(Base):
    __tablename__ = "assistant_steps"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "sequence",
            name="uq_assistant_steps_execution_sequence",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("assistant_executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    input_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    output_json: Mapped[dict[str, object] | None] = mapped_column(
        JSON,
        nullable=True,
    )
    decision_summary: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class AssistantToolCallRecord(Base):
    __tablename__ = "assistant_tool_calls"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_assistant_tool_calls_idempotency_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("assistant_executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("assistant_steps.id", ondelete="SET NULL"),
        nullable=True,
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(32), nullable=False)
    capability: Mapped[str] = mapped_column(String(128), nullable=False)
    effect: Mapped[str] = mapped_column(String(32), nullable=False)
    arguments_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    logical_action_key: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    external_reference_json: Mapped[dict[str, object] | None] = mapped_column(
        JSON,
        nullable=True,
    )
    result_json: Mapped[dict[str, object] | None] = mapped_column(
        JSON,
        nullable=True,
    )
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
    requested_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class AssistantObservationRecord(Base):
    __tablename__ = "assistant_observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("assistant_executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("assistant_steps.id", ondelete="SET NULL"),
        nullable=True,
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    observation_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    evidence_refs_json: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class AssistantHandoffRecord(Base):
    __tablename__ = "assistant_handoffs"
    __table_args__ = (
        UniqueConstraint(
            "source_execution_id",
            name="uq_assistant_handoffs_source_execution",
        ),
        UniqueConstraint(
            "target_execution_id",
            name="uq_assistant_handoffs_target_execution",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_execution_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("assistant_executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_execution_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("assistant_executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("assistant_context_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    grant_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("action_grants.id", ondelete="SET NULL"),
        nullable=True,
    )
    envelope_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class AssistantSubagentRunRecord(Base):
    __tablename__ = "assistant_subagent_runs"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "planning_round",
            "role",
            name="uq_assistant_subagent_runs_execution_round_role",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("assistant_executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    planning_round: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("assistant_context_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )
    task_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    budget_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    result_json: Mapped[dict[str, object] | None] = mapped_column(
        JSON,
        nullable=True,
    )
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class ExternalActionClaimRecord(Base):
    __tablename__ = "external_action_claims"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "capability",
            "logical_action_key",
            name="uq_external_action_claims_logical_action",
        ),
        Index(
            "ix_external_action_claims_status_lease",
            "status",
            "lease_expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    capability: Mapped[str] = mapped_column(String(128), nullable=False)
    logical_action_key: Mapped[str] = mapped_column(String(255), nullable=False)
    holder_execution_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("assistant_executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_call_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("assistant_tool_calls.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    external_reference_json: Mapped[dict[str, object] | None] = mapped_column(
        JSON,
        nullable=True,
    )
    lease_expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class AssistantClientOperationRecord(Base):
    __tablename__ = "assistant_client_operations"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "client_operation_id",
            name="uq_assistant_client_operations_execution_operation",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("assistant_executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    client_operation_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class AssistantEventRecord(Base):
    __tablename__ = "assistant_events"
    __table_args__ = (
        Index("ix_assistant_events_session_cursor", "session_id", "id"),
        Index("ix_assistant_events_execution_cursor", "execution_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("assistant_executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    root_execution_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("assistant_executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    phase: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    payload_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
