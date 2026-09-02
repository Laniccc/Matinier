from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.media.contracts import EventFinality, MediaEvent, MediaMode
from app.persistence.models import (
    MediaBridgeOffsetRecord,
    MediaConsumerCursorRecord,
    MediaEventRecord,
    MediaSessionRecord,
    SessionRecord,
    utc_now,
)


class MediaRepository:
    """Transaction-scoped persistence for generic media sessions and events."""

    def __init__(self, db_session: Session) -> None:
        self._db = db_session

    def ensure_legacy_session_bridge(
        self,
        legacy_session_id: str,
        *,
        mode: MediaMode = "live",
        owner_scope: str | None = None,
    ) -> MediaSessionRecord:
        existing = self._db.scalar(
            select(MediaSessionRecord).where(
                MediaSessionRecord.legacy_session_id == legacy_session_id
            )
        )
        if existing is not None:
            return existing

        legacy = self._db.get(SessionRecord, legacy_session_id)
        if legacy is None:
            raise LookupError(f"legacy Session does not exist: {legacy_session_id}")
        record = MediaSessionRecord(
            id=str(uuid.uuid4()),
            legacy_session_id=legacy_session_id,
            mode=mode,
            source_kind=legacy.source_type,
            status=self._media_status(legacy.status),
            owner_scope=owner_scope,
            next_sequence=1,
        )
        try:
            with self._db.begin_nested():
                self._db.add(record)
                self._db.flush()
        except IntegrityError:
            raced = self._db.scalar(
                select(MediaSessionRecord).where(
                    MediaSessionRecord.legacy_session_id == legacy_session_id
                )
            )
            if raced is None:
                raise
            return raced
        return record

    def append_event(
        self,
        *,
        media_session_id: str,
        event_type: str,
        finality: EventFinality,
        source: str,
        payload: dict[str, Any],
        media_time_ms: int | None = None,
        duration_ms: int | None = None,
        logical_id: str | None = None,
        revision: int | None = None,
        created_at: dt.datetime | None = None,
    ) -> MediaEventRecord:
        if logical_id is not None and revision is not None:
            existing = self._find_source_revision(
                media_session_id=media_session_id,
                source=source,
                logical_id=logical_id,
                revision=revision,
            )
            if existing is not None:
                return existing

        for attempt in range(3):
            event_id = str(uuid.uuid4())
            try:
                with self._db.begin_nested():
                    media_session = self._db.scalar(
                        select(MediaSessionRecord)
                        .where(MediaSessionRecord.id == media_session_id)
                        .with_for_update()
                    )
                    if media_session is None:
                        raise LookupError(
                            f"MediaSession does not exist: {media_session_id}"
                        )
                    sequence = media_session.next_sequence
                    media_session.next_sequence = sequence + 1
                    contract = MediaEvent(
                        event_id=event_id,
                        session_id=media_session_id,
                        sequence=sequence,
                        event_type=event_type,
                        media_time_ms=media_time_ms,
                        duration_ms=duration_ms,
                        logical_id=logical_id,
                        revision=revision,
                        finality=finality,
                        source=source,
                        payload=payload,
                        created_at=created_at or utc_now(),
                    )
                    record = MediaEventRecord(
                        id=contract.event_id,
                        media_session_id=contract.session_id,
                        sequence=contract.sequence,
                        schema_version=contract.schema_version,
                        event_type=contract.event_type,
                        media_time_ms=contract.media_time_ms,
                        duration_ms=contract.duration_ms,
                        logical_id=contract.logical_id,
                        revision=contract.revision,
                        finality=contract.finality,
                        source=contract.source,
                        payload_json=contract.payload,
                        created_at=contract.created_at,
                    )
                    self._db.add(record)
                    self._db.flush()
                return record
            except IntegrityError:
                if logical_id is not None and revision is not None:
                    existing = self._find_source_revision(
                        media_session_id=media_session_id,
                        source=source,
                        logical_id=logical_id,
                        revision=revision,
                    )
                    if existing is not None:
                        return existing
                self._db.expire_all()
                if attempt == 2:
                    raise
        raise RuntimeError("unreachable event allocation state")

    def list_events_after(
        self,
        media_session_id: str,
        *,
        after_sequence: int,
        limit: int = 100,
        consumer_id: str | None = None,
    ) -> list[MediaEventRecord]:
        if after_sequence < 0:
            raise ValueError("after_sequence must not be negative")
        if limit < 1 or limit > 1_000:
            raise ValueError("limit must be between 1 and 1000")
        rows = list(
            self._db.scalars(
                select(MediaEventRecord)
                .where(
                    MediaEventRecord.media_session_id == media_session_id,
                    MediaEventRecord.sequence > after_sequence,
                )
                .order_by(MediaEventRecord.sequence)
                .limit(limit)
            )
        )
        if consumer_id is not None and rows:
            cursor = self.get_or_create_cursor(media_session_id, consumer_id)
            cursor.last_delivered_sequence = max(
                cursor.last_delivered_sequence,
                rows[-1].sequence,
            )
            cursor.updated_at = utc_now()
            self._db.flush()
        return rows

    def get_or_create_cursor(
        self,
        media_session_id: str,
        consumer_id: str,
    ) -> MediaConsumerCursorRecord:
        existing = self._db.scalar(
            select(MediaConsumerCursorRecord).where(
                MediaConsumerCursorRecord.media_session_id == media_session_id,
                MediaConsumerCursorRecord.consumer_id == consumer_id,
            )
        )
        if existing is not None:
            return existing
        if self._db.get(MediaSessionRecord, media_session_id) is None:
            raise LookupError(f"MediaSession does not exist: {media_session_id}")
        record = MediaConsumerCursorRecord(
            id=str(uuid.uuid4()),
            media_session_id=media_session_id,
            consumer_id=consumer_id,
            last_delivered_sequence=0,
            last_acknowledged_sequence=0,
        )
        try:
            with self._db.begin_nested():
                self._db.add(record)
                self._db.flush()
        except IntegrityError:
            raced = self._db.scalar(
                select(MediaConsumerCursorRecord).where(
                    MediaConsumerCursorRecord.media_session_id == media_session_id,
                    MediaConsumerCursorRecord.consumer_id == consumer_id,
                )
            )
            if raced is None:
                raise
            return raced
        return record

    def acknowledge_cursor(
        self,
        media_session_id: str,
        consumer_id: str,
        *,
        sequence: int,
    ) -> MediaConsumerCursorRecord:
        if sequence < 0:
            raise ValueError("acknowledged sequence must not be negative")
        cursor = self.get_or_create_cursor(media_session_id, consumer_id)
        if sequence > cursor.last_delivered_sequence:
            raise ValueError("cannot acknowledge an event that was not delivered")
        if sequence > cursor.last_acknowledged_sequence:
            cursor.last_acknowledged_sequence = sequence
            cursor.updated_at = utc_now()
            self._db.flush()
        return cursor

    def get_bridge_offset(
        self,
        media_session_id: str,
        *,
        source_table: str,
        source_type: str,
        source_logical_id: str,
    ) -> MediaBridgeOffsetRecord | None:
        return self._db.scalar(
            select(MediaBridgeOffsetRecord).where(
                MediaBridgeOffsetRecord.media_session_id == media_session_id,
                MediaBridgeOffsetRecord.source_table == source_table,
                MediaBridgeOffsetRecord.source_type == source_type,
                MediaBridgeOffsetRecord.source_logical_id == source_logical_id,
            )
        )

    def advance_bridge_offset(
        self,
        media_session_id: str,
        *,
        source_table: str,
        source_type: str,
        source_logical_id: str,
        processed_revision: int,
        processed_at: dt.datetime | None = None,
    ) -> MediaBridgeOffsetRecord:
        if processed_revision < 1:
            raise ValueError("processed_revision must be positive")
        existing = self.get_bridge_offset(
            media_session_id,
            source_table=source_table,
            source_type=source_type,
            source_logical_id=source_logical_id,
        )
        timestamp = processed_at or utc_now()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("processed_at must be timezone-aware")
        timestamp = timestamp.astimezone(dt.UTC)
        if existing is not None:
            if processed_revision > existing.processed_revision:
                existing.processed_revision = processed_revision
                existing.processed_at = timestamp
                self._db.flush()
            return existing
        record = MediaBridgeOffsetRecord(
            id=str(uuid.uuid4()),
            media_session_id=media_session_id,
            source_table=source_table,
            source_type=source_type,
            source_logical_id=source_logical_id,
            processed_revision=processed_revision,
            processed_at=timestamp,
        )
        self._db.add(record)
        self._db.flush()
        return record

    def _find_source_revision(
        self,
        *,
        media_session_id: str,
        source: str,
        logical_id: str,
        revision: int,
    ) -> MediaEventRecord | None:
        return self._db.scalar(
            select(MediaEventRecord).where(
                MediaEventRecord.media_session_id == media_session_id,
                MediaEventRecord.source == source,
                MediaEventRecord.logical_id == logical_id,
                MediaEventRecord.revision == revision,
            )
        )

    @staticmethod
    def _media_status(legacy_status: str) -> str:
        if legacy_status in {"ended", "completed", "stopped"}:
            return "completed"
        if legacy_status in {"failed", "error"}:
            return "failed"
        return "active"

