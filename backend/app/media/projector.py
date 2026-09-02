from __future__ import annotations

import asyncio
import datetime as dt
import logging
import threading
from dataclasses import dataclass

from sqlalchemy import func, select

from app.media.repository import MediaRepository
from app.persistence.database import Database
from app.persistence.models import (
    MediaBridgeOffsetRecord,
    MediaEventRecord,
    MediaSessionRecord,
    SegmentRecord,
    SessionRecord,
    TranslationSegmentRecord,
    utc_now,
)
from app.sessions.state import TERMINAL_SESSION_STATES


logger = logging.getLogger(__name__)


def _aware(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _duration(start_ms: int | None, end_ms: int | None) -> int | None:
    if start_ms is None or end_ms is None:
        return None
    return max(0, end_ms - start_ms)


@dataclass(frozen=True, slots=True)
class ProjectorConfig:
    poll_interval_ms: int = 500
    batch_size: int = 100
    scan_session_limit: int = 100

    def __post_init__(self) -> None:
        if self.poll_interval_ms <= 0:
            raise ValueError("MediaEvent poll interval must be positive")
        if self.batch_size <= 0 or self.batch_size > 1_000:
            raise ValueError("MediaEvent batch size must be between 1 and 1000")
        if self.scan_session_limit <= 0 or self.scan_session_limit > 10_000:
            raise ValueError("MediaEvent session scan limit must be between 1 and 10000")


@dataclass(frozen=True, slots=True)
class MediaCatchUpResult:
    legacy_session_id: str
    media_session_id: str
    projected_event_count: int
    latest_sequence: int


@dataclass(frozen=True, slots=True)
class _ProjectionItem:
    source_table: str
    source_type: str
    source_logical_id: str
    event_type: str
    logical_id: str
    revision: int
    media_time_ms: int | None
    duration_ms: int | None
    source: str
    payload: dict[str, object]
    created_at: dt.datetime
    terminal_status: str | None = None


class MediaEventProjector:
    """Projects durable host records into the generic MediaEvent sidecar."""

    def __init__(self, database: Database, *, config: ProjectorConfig) -> None:
        self._database = database
        self._config = config
        self._poll_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._projection_lock = threading.Lock()

    @property
    def started(self) -> bool:
        return self._poll_task is not None and not self._poll_task.done()

    async def start(self) -> None:
        if self.started:
            return
        self._stopping = False
        self._poll_task = asyncio.create_task(
            self._poll_loop(),
            name="media-event-projector",
        )
        logger.info(
            "MediaEvent projector started",
            extra={"event": "media_event_projector_started"},
        )

    async def stop(self) -> None:
        task = self._poll_task
        if task is None:
            return
        self._stopping = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._poll_task = None
        logger.info(
            "MediaEvent projector stopped",
            extra={"event": "media_event_projector_stopped"},
        )

    async def request_catch_up(self, legacy_session_id: str) -> MediaCatchUpResult:
        return await asyncio.to_thread(
            self._project_session_serialized,
            legacy_session_id,
        )

    async def _poll_loop(self) -> None:
        while True:
            try:
                session_ids = await asyncio.to_thread(self._list_session_ids)
                for session_id in session_ids:
                    if self._stopping:
                        return
                    await asyncio.to_thread(
                        self._project_session_serialized,
                        session_id,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error(
                    "MediaEvent projection scan failed",
                    extra={
                        "event": "media_event_projection_scan_failed",
                        "internal_error_type": type(error).__name__,
                    },
                )
            await asyncio.sleep(self._config.poll_interval_ms / 1_000)

    def _project_session_serialized(
        self,
        legacy_session_id: str,
    ) -> MediaCatchUpResult:
        # SQLite is a single-writer store and one projection spans separate event and
        # offset transactions. Keep the complete projection atomic with respect to
        # other in-process scans, including workers whose awaiting task was cancelled.
        with self._projection_lock:
            return self._project_session_to_completion(legacy_session_id)

    def _list_session_ids(self) -> list[str]:
        with self._database.session() as db_session:
            return list(
                db_session.scalars(
                    select(SessionRecord.id)
                    .order_by(SessionRecord.created_at, SessionRecord.id)
                    .limit(self._config.scan_session_limit)
                )
            )

    def _project_session_to_completion(
        self,
        legacy_session_id: str,
    ) -> MediaCatchUpResult:
        total_projected = 0
        media_session_id: str | None = None
        while True:
            items = self._read_pending_items(legacy_session_id)
            if not items:
                media_session_id = self._ensure_bridge(legacy_session_id)
                break
            processed_items = items[: self._config.batch_size]
            media_session_id, projected = self._persist_event_batch(
                legacy_session_id,
                processed_items,
            )
            total_projected += projected
            self._advance_offsets(media_session_id, processed_items)
            if len(processed_items) < self._config.batch_size:
                break

        assert media_session_id is not None
        with self._database.session() as db_session:
            latest = int(
                db_session.scalar(
                    select(func.max(MediaEventRecord.sequence)).where(
                        MediaEventRecord.media_session_id == media_session_id
                    )
                )
                or 0
            )
        return MediaCatchUpResult(
            legacy_session_id=legacy_session_id,
            media_session_id=media_session_id,
            projected_event_count=total_projected,
            latest_sequence=latest,
        )

    def _ensure_bridge(self, legacy_session_id: str) -> str:
        with self._database.session() as db_session:
            repository = MediaRepository(db_session)
            record = repository.ensure_legacy_session_bridge(legacy_session_id)
            db_session.commit()
            return record.id

    def _read_pending_items(self, legacy_session_id: str) -> list[_ProjectionItem]:
        with self._database.session() as db_session:
            legacy = db_session.get(SessionRecord, legacy_session_id)
            if legacy is None:
                raise LookupError(f"Session not found: {legacy_session_id}")
            media_session = db_session.scalar(
                select(MediaSessionRecord).where(
                    MediaSessionRecord.legacy_session_id == legacy_session_id
                )
            )
            offsets: dict[tuple[str, str, str], int] = {}
            if media_session is not None:
                offsets = {
                    (
                        row.source_table,
                        row.source_type,
                        row.source_logical_id,
                    ): row.processed_revision
                    for row in db_session.scalars(
                        select(MediaBridgeOffsetRecord).where(
                            MediaBridgeOffsetRecord.media_session_id == media_session.id
                        )
                    )
                }

            items: list[_ProjectionItem] = []
            segments = list(
                db_session.scalars(
                    select(SegmentRecord)
                    .where(
                        SegmentRecord.session_id == legacy_session_id,
                        SegmentRecord.status == "final",
                    )
                    .order_by(
                        SegmentRecord.audio_start_ms.is_(None),
                        SegmentRecord.audio_start_ms,
                        SegmentRecord.finalized_at,
                        SegmentRecord.id,
                    )
                )
            )
            for record in segments:
                source_logical_id = f"transcript:{record.segment_id}"
                if offsets.get(("segments", "transcript.final", source_logical_id), 0) >= record.revision:
                    continue
                items.append(
                    _ProjectionItem(
                        source_table="segments",
                        source_type="transcript.final",
                        source_logical_id=source_logical_id,
                        event_type="transcript.final",
                        logical_id=source_logical_id,
                        revision=record.revision,
                        media_time_ms=record.audio_start_ms,
                        duration_ms=_duration(record.audio_start_ms, record.audio_end_ms),
                        source="host.transcription",
                        payload={
                            "text": record.display_text,
                            "language": record.language,
                            "track_id": record.track_id,
                            "confidence": record.confidence,
                            "evidence": {
                                "legacy_session_id": record.session_id,
                                "segment_id": record.segment_id,
                                "record_id": record.id,
                            },
                        },
                        created_at=_aware(record.finalized_at),
                    )
                )

            translations = list(
                db_session.scalars(
                    select(TranslationSegmentRecord)
                    .where(
                        TranslationSegmentRecord.session_id == legacy_session_id,
                        TranslationSegmentRecord.status == "final",
                    )
                    .order_by(
                        TranslationSegmentRecord.audio_start_ms.is_(None),
                        TranslationSegmentRecord.audio_start_ms,
                        TranslationSegmentRecord.finalized_at,
                        TranslationSegmentRecord.id,
                    )
                )
            )
            for record in translations:
                source_logical_id = (
                    f"translation:{record.target_language}:{record.segment_id}"
                )
                if offsets.get(
                    ("translation_segments", "translation.final", source_logical_id),
                    0,
                ) >= record.revision:
                    continue
                items.append(
                    _ProjectionItem(
                        source_table="translation_segments",
                        source_type="translation.final",
                        source_logical_id=source_logical_id,
                        event_type="translation.final",
                        logical_id=source_logical_id,
                        revision=record.revision,
                        media_time_ms=record.audio_start_ms,
                        duration_ms=_duration(record.audio_start_ms, record.audio_end_ms),
                        source="host.translation",
                        payload={
                            "text": record.text,
                            "language": record.target_language,
                            "confidence": None,
                            "source_language": record.source_language,
                            "target_language": record.target_language,
                            "source_segment_ids": list(record.source_segment_ids),
                            "evidence": {
                                "legacy_session_id": record.session_id,
                                "translation_segment_id": record.segment_id,
                                "record_id": record.id,
                            },
                        },
                        created_at=_aware(record.finalized_at),
                    )
                )

            terminal_status = (
                legacy.status if legacy.status in TERMINAL_SESSION_STATES else None
            )
            if terminal_status is not None:
                source_logical_id = f"session:{legacy.id}:terminal"
                if offsets.get(
                    ("sessions", f"session.{terminal_status}", source_logical_id),
                    0,
                ) < 1:
                    items.append(
                        _ProjectionItem(
                            source_table="sessions",
                            source_type=f"session.{terminal_status}",
                            source_logical_id=source_logical_id,
                            event_type=f"session.{terminal_status}",
                            logical_id=source_logical_id,
                            revision=1,
                            media_time_ms=None,
                            duration_ms=None,
                            source="host.session",
                            payload={
                                "legacy_session_id": legacy.id,
                                "status": terminal_status,
                                "stop_reason": legacy.stop_reason,
                            },
                            created_at=_aware(
                                legacy.ended_at or legacy.created_at or utc_now()
                            ),
                            terminal_status=terminal_status,
                        )
                    )
            return items

    def _persist_event_batch(
        self,
        legacy_session_id: str,
        items: list[_ProjectionItem],
    ) -> tuple[str, int]:
        with self._database.session() as db_session:
            repository = MediaRepository(db_session)
            media_session = repository.ensure_legacy_session_bridge(legacy_session_id)
            projected = 0
            for item in items:
                existing = db_session.scalar(
                    select(MediaEventRecord.id).where(
                        MediaEventRecord.media_session_id == media_session.id,
                        MediaEventRecord.source == item.source,
                        MediaEventRecord.logical_id == item.logical_id,
                        MediaEventRecord.revision == item.revision,
                    )
                )
                repository.append_event(
                    media_session_id=media_session.id,
                    event_type=item.event_type,
                    media_time_ms=item.media_time_ms,
                    duration_ms=item.duration_ms,
                    logical_id=item.logical_id,
                    revision=item.revision,
                    finality="final" if item.source_table != "sessions" else "derived",
                    source=item.source,
                    payload=item.payload,
                    created_at=item.created_at,
                )
                if existing is None:
                    projected += 1
            db_session.commit()
            return media_session.id, projected

    def _advance_offsets(
        self,
        media_session_id: str,
        items: list[_ProjectionItem],
    ) -> None:
        with self._database.session() as db_session:
            repository = MediaRepository(db_session)
            terminal_status: str | None = None
            for item in items:
                repository.advance_bridge_offset(
                    media_session_id,
                    source_table=item.source_table,
                    source_type=item.source_type,
                    source_logical_id=item.source_logical_id,
                    processed_revision=item.revision,
                    processed_at=utc_now(),
                )
                terminal_status = item.terminal_status or terminal_status
            if terminal_status is not None:
                media_session = db_session.get(MediaSessionRecord, media_session_id)
                if media_session is not None:
                    media_session.status = terminal_status
                    media_session.updated_at = utc_now()
            db_session.commit()
