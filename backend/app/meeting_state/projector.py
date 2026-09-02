from __future__ import annotations

import asyncio
import datetime as dt
import heapq
import itertools
import logging
from dataclasses import dataclass, field
from types import MappingProxyType
from collections.abc import Sequence
from typing import Mapping

from pydantic import ValidationError
from sqlalchemy import func, or_, select

from app.assistant.plugin_policy import AnalysisAdmission, MeetingPluginPolicy
from app.assistant.plugin_repository import MeetingPluginDenied

from app.meeting_state.candidates import CandidateMergePolicy
from app.meeting_state.contracts import (
    CandidateOperation,
    MeetingStateDelta,
    ProjectionSegment,
)
from app.meeting_state.extractor import MeetingStateExtractor
from app.meeting_state.merge import canonical_state_hash, merge_meeting_state
from app.meeting_state.models import MeetingState
from app.meeting_state.repository import (
    MeetingStateConflictError,
    MeetingStateRepository,
)
from app.persistence.database import Database
from app.persistence.models import (
    MeetingProjectionOffsetRecord,
    MeetingStateHeadRecord,
    SegmentRecord,
    SessionRecord,
    utc_now,
)


logger = logging.getLogger(__name__)
_FINALIZING_STATUSES = frozenset(
    {"finalizing", "completed", "failed", "cancelled"}
)


def _aware(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _later(
    first: dt.datetime | None,
    second: dt.datetime | None,
) -> dt.datetime | None:
    if first is None:
        return second
    if second is None:
        return first
    return first if _aware(first) >= _aware(second) else second


@dataclass(frozen=True, slots=True)
class ProjectorConfig:
    poll_interval_ms: int = 400
    batch_segments: int = 10
    batch_trigger_chars: int = 2_000
    max_input_chars: int = 12_000
    max_batch_wait_seconds: float = 4.0
    scan_overlap_seconds: float = 2.0
    stale_after_seconds: float = 10.0
    concurrency: int = 2
    priority_burst: int = 2
    retry_delays_seconds: tuple[float, ...] = (1, 2, 5, 10, 30)

    def __post_init__(self) -> None:
        positive = (
            self.poll_interval_ms,
            self.batch_segments,
            self.batch_trigger_chars,
            self.max_input_chars,
            self.max_batch_wait_seconds,
            self.stale_after_seconds,
            self.concurrency,
            self.priority_burst,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("Meeting State projector limits must be positive")
        if self.scan_overlap_seconds < 0:
            raise ValueError("Meeting State overlap must be non-negative")
        if not self.retry_delays_seconds or any(
            value <= 0 for value in self.retry_delays_seconds
        ):
            raise ValueError("Meeting State retry delays must be positive")


@dataclass(frozen=True, slots=True)
class CatchUpResult:
    session_id: str
    state_version: int
    source_frontier: Mapping[str, object]


class CatchUpTarget:
    def __init__(
        self,
        *,
        session_id: str,
        target_revisions: Mapping[str, int],
        future: asyncio.Future[CatchUpResult],
        analysis_epoch: int,
    ) -> None:
        self.session_id = session_id
        self.target_revisions = MappingProxyType(dict(target_revisions))
        self._future = future
        self.analysis_epoch = analysis_epoch

    @property
    def done(self) -> bool:
        return self._future.done()

    async def wait(self, timeout: float | None = None) -> CatchUpResult:
        awaitable = asyncio.shield(self._future)
        if timeout is None:
            return await awaitable
        return await asyncio.wait_for(awaitable, timeout=timeout)


@dataclass(slots=True)
class _SessionBuffer:
    analysis_epoch: int = -1
    segments: dict[str, ProjectionSegment] = field(default_factory=dict)
    first_pending_at: float | None = None
    finalizing: bool = False
    priority_requested: bool = False
    failure_count: int = 0
    retry_at: float = 0.0


@dataclass(order=True, frozen=True, slots=True)
class _QueueEntry:
    queued_at: float
    sequence: int
    session_id: str = field(compare=False)


class _FairSessionQueue:
    def __init__(self, priority_burst: int) -> None:
        self._priority_burst = priority_burst
        self._priority: list[_QueueEntry] = []
        self._normal: list[_QueueEntry] = []
        self._scheduled: dict[str, bool] = {}
        self._sequence = itertools.count()
        self._priority_streak = 0
        self._condition = asyncio.Condition()

    async def put(self, session_id: str, *, priority: bool) -> None:
        async with self._condition:
            current = self._scheduled.get(session_id)
            if current is True or current is priority:
                return
            self._scheduled[session_id] = priority
            entry = _QueueEntry(
                queued_at=asyncio.get_running_loop().time(),
                sequence=next(self._sequence),
                session_id=session_id,
            )
            heapq.heappush(self._priority if priority else self._normal, entry)
            self._condition.notify()

    async def get(self) -> tuple[str, bool]:
        async with self._condition:
            while True:
                use_priority = bool(self._priority) and not (
                    self._normal
                    and self._priority_streak >= self._priority_burst
                )
                heap = self._priority if use_priority else self._normal
                if not heap:
                    await self._condition.wait()
                    continue
                entry = heapq.heappop(heap)
                scheduled_priority = self._scheduled.get(entry.session_id)
                if scheduled_priority is None or scheduled_priority is not use_priority:
                    continue
                del self._scheduled[entry.session_id]
                if use_priority:
                    self._priority_streak += 1
                else:
                    self._priority_streak = 0
                return entry.session_id, use_priority


class MeetingStateProjector:
    """Incrementally projects Final captions without touching their hot path."""

    def __init__(
        self,
        database: Database,
        extractor: MeetingStateExtractor,
        *,
        config: ProjectorConfig,
        policy: MeetingPluginPolicy | None = None,
    ) -> None:
        self._database = database
        self._extractor = extractor
        self._config = config
        self._policy = policy or MeetingPluginPolicy(database)
        self._seen_epochs: dict[str, int] = {}
        self._queue = _FairSessionQueue(config.priority_burst)
        self._buffers: dict[str, _SessionBuffer] = {}
        self._inflight: set[str] = set()
        self._waiters: dict[str, list[CatchUpTarget]] = {}
        self._scan_cursor: dt.datetime | None = None
        self._lock = asyncio.Lock()
        self._poll_task: asyncio.Task[None] | None = None
        self._workers: list[asyncio.Task[None]] = []
        self._stopping = False

    @property
    def started(self) -> bool:
        return self._poll_task is not None and not self._poll_task.done()

    async def start(self) -> None:
        if self.started:
            return
        self._stopping = False
        self._poll_task = asyncio.create_task(
            self._poll_loop(),
            name="meeting-state-poller",
        )
        self._workers = [
            asyncio.create_task(
                self._worker(),
                name=f"meeting-state-worker-{index + 1}",
            )
            for index in range(self._config.concurrency)
        ]
        logger.info(
            "Meeting State projector started",
            extra={
                "event": "meeting_state_projector_started",
                "concurrency": self._config.concurrency,
            },
        )

    async def stop(self) -> None:
        tasks = [
            task
            for task in (self._poll_task, *self._workers)
            if task is not None
        ]
        if not tasks:
            return
        self._stopping = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._poll_task = None
        self._workers.clear()
        self._inflight.clear()
        self._buffers.clear()
        self._seen_epochs.clear()
        self._queue = _FairSessionQueue(self._config.priority_burst)
        for targets in self._waiters.values():
            for target in targets:
                if not target._future.done():
                    target._future.cancel()
        self._waiters.clear()
        logger.info(
            "Meeting State projector stopped",
            extra={"event": "meeting_state_projector_stopped"},
        )

    async def request_catch_up(self, session_id: str) -> CatchUpTarget:
        if not self.started or self._stopping:
            raise RuntimeError("Meeting State projector is not available")
        segments, target_revisions = self._read_session_backlog(session_id)
        admission = self._require_analysis(session_id)
        future = asyncio.get_running_loop().create_future()
        target = CatchUpTarget(
            session_id=session_id,
            target_revisions=target_revisions,
            future=future,
            analysis_epoch=admission.analysis_epoch,
        )
        self._waiters.setdefault(session_id, []).append(target)
        await self._buffer_segments(segments)
        async with self._lock:
            buffer = self._buffers.setdefault(session_id, _SessionBuffer())
            buffer.analysis_epoch = admission.analysis_epoch
            buffer.priority_requested = True
        self._resolve_waiters(session_id)
        if not target.done:
            await self._schedule_if_ready(session_id, force_priority=True)
        return target

    def can_request_catch_up(self, session_id: str) -> bool:
        admission = self._policy.admission(session_id)
        return self.started and not self._stopping and admission.allowed and admission.state == "active"

    def _require_analysis(self, session_id: str, epoch: int | None = None) -> AnalysisAdmission:
        admission = self._policy.admission(session_id)
        if not admission.allowed or (epoch is not None and not admission.permits(epoch)):
            raise MeetingPluginDenied("meeting analysis is not active for this epoch")
        return admission

    async def _discard_buffer(self, session_id: str, epoch: int):
        async with self._lock:
            buffer = self._buffers.get(session_id)
            if buffer is not None and buffer.analysis_epoch == epoch:
                self._buffers.pop(session_id, None)
        retained = []
        for target in self._waiters.get(session_id, ()):
            if target.analysis_epoch == epoch:
                target._future.cancel()
            else:
                retained.append(target)
        if retained:
            self._waiters[session_id] = retained
        else:
            self._waiters.pop(session_id, None)

    async def _poll_loop(self) -> None:
        while True:
            try:
                segments, finalizing_sessions = self._scan_updates()
                await self._buffer_segments(segments)
                async with self._lock:
                    for session_id in finalizing_sessions:
                        self._buffers.setdefault(
                            session_id,
                            _SessionBuffer(),
                        ).finalizing = True
                    session_ids = tuple(self._buffers)
                for session_id in session_ids:
                    await self._schedule_if_ready(session_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error(
                    "Meeting State polling failed",
                    extra={
                        "event": "meeting_state_poll_failed",
                        "internal_error_type": type(error).__name__,
                    },
                )
            await asyncio.sleep(self._config.poll_interval_ms / 1_000)

    def _scan_updates(self) -> tuple[list[ProjectionSegment], set[str]]:
        admissions = self._policy.scan_admissions()
        # Terminal draining is bounded by a frozen frontier, not the live
        # cursor. An in-flight batch can be discarded during the transition.
        backfill = {
            key for key, value in admissions.items()
            if self._seen_epochs.get(key) != value.analysis_epoch
            or value.state == "draining"
        }
        with self._database.session() as db_session:
            offset_match = (
                (MeetingProjectionOffsetRecord.session_id == SegmentRecord.session_id)
                & (MeetingProjectionOffsetRecord.segment_id == SegmentRecord.segment_id)
            )
            statement = (
                select(SegmentRecord)
                .outerjoin(MeetingProjectionOffsetRecord, offset_match)
                .where(
                    SegmentRecord.status == "final",
                    SegmentRecord.session_id.in_(tuple(admissions)),
                    or_(
                        MeetingProjectionOffsetRecord.processed_revision.is_(None),
                        MeetingProjectionOffsetRecord.processed_revision
                        < SegmentRecord.revision,
                    ),
                )
                .order_by(SegmentRecord.updated_at, SegmentRecord.id)
            )
            if self._scan_cursor is not None:
                boundary = self._scan_cursor - dt.timedelta(
                    seconds=self._config.scan_overlap_seconds
                )
                statement = statement.where(or_(
                    SegmentRecord.updated_at >= boundary,
                    SegmentRecord.session_id.in_(tuple(backfill)),
                ))
            records = list(db_session.scalars(statement))
            finalizing = set(
                db_session.scalars(
                    select(SessionRecord.id).where(
                        SessionRecord.status.in_(_FINALIZING_STATUSES),
                        SessionRecord.id.in_(tuple(admissions)),
                    )
                )
            )

        observed = max(
            (record.updated_at for record in records),
            default=utc_now(),
            key=_aware,
        )
        self._scan_cursor = _later(self._scan_cursor, observed)
        records = [record for record in records if admissions[record.session_id].permits_segment(record)]
        snapshots = self._validated_snapshots(records, admissions=admissions)
        self._seen_epochs = {key: value.analysis_epoch for key, value in admissions.items()}
        return snapshots, finalizing

    def _read_session_backlog(
        self,
        session_id: str,
    ) -> tuple[list[ProjectionSegment], dict[str, int]]:
        with self._database.session() as db_session:
            admission = self._policy.admission(session_id, db=db_session)
            if not admission.allowed:
                raise MeetingPluginDenied("meeting analysis is not active")
            session = db_session.get(SessionRecord, session_id)
            if session is None:
                raise LookupError(f"Session not found: {session_id}")
            records = list(
                db_session.scalars(
                    select(SegmentRecord)
                    .where(
                        SegmentRecord.session_id == session_id,
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
            offsets = {
                record.segment_id: record.processed_revision
                for record in db_session.scalars(
                    select(MeetingProjectionOffsetRecord).where(
                        MeetingProjectionOffsetRecord.session_id == session_id
                    )
                )
            }
        records = [record for record in records if admission.permits_segment(record)]
        pending_records = [
            record
            for record in records
            if offsets.get(record.segment_id, 0) < record.revision
        ]
        backlog = self._validated_snapshots(pending_records, admissions={session_id: admission})
        return backlog, {record.segment_id: record.revision for record in records}

    def _validated_snapshots(
        self,
        records: Sequence[SegmentRecord],
        *,
        admissions: Mapping[str, AnalysisAdmission] | None = None,
    ) -> list[ProjectionSegment]:
        snapshots: list[ProjectionSegment] = []
        invalid: list[SegmentRecord] = []
        for record in records:
            try:
                snapshots.append(self._snapshot(record))
            except ValidationError:
                invalid.append(record)
        if not invalid:
            return snapshots

        now = utc_now()
        for session_id in {record.session_id for record in invalid}:
            admission = (admissions or {}).get(session_id) or self._policy.admission(session_id)
            with self._database.session() as db_session:
                try:
                    current = self._policy.fence(db_session, session_id, admission.analysis_epoch)
                except MeetingPluginDenied:
                    continue
                repository = MeetingStateRepository(db_session)
                for record in invalid:
                    if record.session_id == session_id and current.permits_segment(record):
                        repository.advance_offset(session_id=session_id, segment_id=record.segment_id, processed_revision=record.revision, processed_at=now)
                db_session.commit()
        logger.warning(
            "Skipped invalid legacy Final captions during Meeting State projection",
            extra={
                "event": "meeting_state_invalid_finals_skipped",
                "session_ids": sorted({record.session_id for record in invalid}),
                "invalid_segment_count": len(invalid),
            },
        )
        return snapshots

    @staticmethod
    def _snapshot(record: SegmentRecord) -> ProjectionSegment:
        return ProjectionSegment(
            session_id=record.session_id,
            segment_id=record.segment_id,
            revision=record.revision,
            track_id=record.track_id,
            language=record.language,
            raw_text=record.raw_text,
            display_text=record.display_text,
            audio_start_ms=record.audio_start_ms,
            audio_end_ms=record.audio_end_ms,
            confidence=record.confidence,
            received_at_ms=record.received_at_ms,
            finalized_at=record.finalized_at,
            updated_at=record.updated_at,
        )

    async def _buffer_segments(self, segments: list[ProjectionSegment]) -> None:
        if not segments:
            return
        now = asyncio.get_running_loop().time()
        admissions = {key: self._policy.admission(key) for key in {s.session_id for s in segments}}
        async with self._lock:
            for segment in segments:
                admission = admissions[segment.session_id]
                if not admission.permits_segment(segment):
                    continue
                existing = self._buffers.get(segment.session_id)
                if existing is not None and existing.analysis_epoch != admission.analysis_epoch:
                    self._buffers.pop(segment.session_id)
                buffer = self._buffers.setdefault(
                    segment.session_id,
                    _SessionBuffer(analysis_epoch=admission.analysis_epoch),
                )
                current = buffer.segments.get(segment.segment_id)
                if current is None or segment.revision > current.revision:
                    if not buffer.segments:
                        buffer.first_pending_at = now
                    buffer.segments[segment.segment_id] = segment

    async def _schedule_if_ready(
        self,
        session_id: str,
        *,
        force_priority: bool = False,
    ) -> None:
        buffer = self._buffers.get(session_id)
        if buffer is not None and not self._policy.admission(session_id).permits(buffer.analysis_epoch):
            await self._discard_buffer(session_id, buffer.analysis_epoch)
            return
        loop_time = asyncio.get_running_loop().time()
        async with self._lock:
            buffer = self._buffers.get(session_id)
            if (
                buffer is None
                or not buffer.segments
                or session_id in self._inflight
                or loop_time < buffer.retry_at
            ):
                return
            char_count = sum(
                len(segment.display_text) for segment in buffer.segments.values()
            )
            age = (
                loop_time - buffer.first_pending_at
                if buffer.first_pending_at is not None
                else 0
            )
            priority = force_priority or buffer.priority_requested
            ready = (
                priority
                or buffer.finalizing
                or len(buffer.segments) >= self._config.batch_segments
                or char_count >= self._config.batch_trigger_chars
                or age >= self._config.max_batch_wait_seconds
            )
        if ready:
            await self._queue.put(session_id, priority=priority)

    async def _worker(self) -> None:
        while True:
            session_id, _priority = await self._queue.get()
            async with self._lock:
                if session_id in self._inflight:
                    continue
                buffer = self._buffers.get(session_id)
                if buffer is None or not buffer.segments:
                    continue
                self._inflight.add(session_id)
            try:
                await self._project_one_batch(session_id)
            finally:
                async with self._lock:
                    self._inflight.discard(session_id)
                await self._schedule_if_ready(session_id)

    async def _project_one_batch(self, session_id: str) -> None:
        buffer = self._buffers.get(session_id)
        if buffer is None:
            return
        epoch = buffer.analysis_epoch
        try:
            self._require_analysis(session_id, epoch)
            batch = await self._select_batch(session_id)
            if not batch:
                return
            current, expected_version = self._read_current_state(session_id)
            delta = await self._extract_batch_delta(current, batch, analysis_epoch=epoch)
            self._commit_projection(
                current=current,
                expected_version=expected_version,
                delta=delta,
                batch=batch,
                analysis_epoch=epoch,
            )
        except asyncio.CancelledError:
            raise
        except MeetingPluginDenied:
            await self._discard_buffer(session_id, epoch)
            return
        except Exception as error:
            if not self._policy.admission(session_id).permits(epoch):
                await self._discard_buffer(session_id, epoch)
                return
            await self._record_failure(session_id, error, analysis_epoch=epoch)
            return
        await self._acknowledge(session_id, batch, analysis_epoch=epoch)
        self._resolve_waiters(session_id)
        self._policy.finish_if_drained(session_id, epoch)

    async def _select_batch(self, session_id: str) -> tuple[ProjectionSegment, ...]:
        async with self._lock:
            buffer = self._buffers.get(session_id)
            if buffer is None:
                return ()
            ordered = sorted(
                buffer.segments.values(),
                key=lambda value: (
                    value.audio_start_ms is None,
                    value.audio_start_ms or 0,
                    _aware(value.finalized_at),
                    value.segment_id,
                ),
            )
        selected: list[ProjectionSegment] = []
        char_count = 0
        for segment in ordered:
            next_count = char_count + len(segment.display_text)
            if selected and next_count > self._config.max_input_chars:
                break
            selected.append(segment)
            char_count = next_count
            if char_count >= self._config.max_input_chars:
                break
        return tuple(selected)

    async def _extract_batch_delta(
        self,
        state: MeetingState,
        batch: tuple[ProjectionSegment, ...],
        *,
        analysis_epoch: int,
    ) -> MeetingStateDelta:
        self._require_batch(state.session_id, analysis_epoch, batch)
        if len(batch) != 1 or len(batch[0].display_text) <= self._config.max_input_chars:
            return await self._extractor.extract(state=state, segments=batch)

        segment = batch[0]
        deltas: list[MeetingStateDelta] = []
        for fragment in self._split_text(
            segment.display_text,
            self._config.max_input_chars,
        ):
            self._require_batch(state.session_id, analysis_epoch, batch)
            projected_fragment = segment.model_copy(
                update={"raw_text": fragment, "display_text": fragment}
            )
            deltas.append(
                await self._extractor.extract(
                    state=state,
                    segments=(projected_fragment,),
                )
            )
        return MeetingStateDelta(
            session_id=state.session_id,
            source_segment_ids=(segment.segment_id,),
            topics=tuple(item for delta in deltas for item in delta.topics),
            entities=tuple(item for delta in deltas for item in delta.entities),
            decisions=tuple(item for delta in deltas for item in delta.decisions),
            highlights=tuple(item for delta in deltas for item in delta.highlights),
            conflicts=tuple(item for delta in deltas for item in delta.conflicts),
            action_operations=self._coalesce_action_operations(
                tuple(
                    operation
                    for delta in deltas
                    for operation in delta.action_operations
                )
            ),
            warnings=tuple(warning for delta in deltas for warning in delta.warnings),
        )

    def _require_batch(
        self,
        session_id: str,
        epoch: int,
        batch: tuple[ProjectionSegment, ...],
    ) -> None:
        admission = self._require_analysis(session_id, epoch)
        if any(not admission.permits_segment(segment) for segment in batch):
            raise MeetingPluginDenied("batch exceeds the terminal frontier")

    @staticmethod
    def _split_text(value: str, limit: int) -> tuple[str, ...]:
        remaining = value.strip()
        fragments: list[str] = []
        while len(remaining) > limit:
            boundary = remaining.rfind(" ", 0, limit + 1)
            if boundary <= 0:
                boundary = limit
            fragments.append(remaining[:boundary].strip())
            remaining = remaining[boundary:].strip()
        if remaining:
            fragments.append(remaining)
        return tuple(fragments)

    @staticmethod
    def _coalesce_action_operations(
        operations: Sequence[CandidateOperation],
    ) -> tuple[CandidateOperation, ...]:
        retained: list[CandidateOperation] = []
        touched_candidate_ids: set[str] = set()
        for operation in reversed(operations):
            if operation.kind == "create":
                retained.append(operation)
                continue
            if operation.kind == "merge":
                candidate_ids = set(operation.candidate_ids)
            else:
                candidate_ids = {operation.candidate_id}
            if candidate_ids & touched_candidate_ids:
                continue
            touched_candidate_ids.update(candidate_ids)
            retained.append(operation)
        retained.reverse()
        return tuple(retained)

    def _read_current_state(self, session_id: str) -> tuple[MeetingState, int]:
        with self._database.session() as db_session:
            head = MeetingStateRepository(db_session).get_head(session_id)
            if head is None:
                return MeetingState(session_id=session_id, version=0), 0
            return MeetingState.model_validate(head.state_json), head.version

    def _commit_projection(
        self,
        *,
        current: MeetingState,
        expected_version: int,
        delta: MeetingStateDelta,
        batch: tuple[ProjectionSegment, ...],
        analysis_epoch: int,
    ) -> None:
        now = utc_now()
        session_id = current.session_id
        with self._database.session() as db_session:
            admission = self._policy.fence(db_session, session_id, analysis_epoch)
            if any(not admission.permits_segment(segment) for segment in batch):
                raise MeetingPluginDenied("batch exceeds the terminal frontier")
            repository = MeetingStateRepository(db_session)
            live_head = repository.get_head(session_id)
            live_version = live_head.version if live_head is not None else 0
            if live_version != expected_version:
                raise MeetingStateConflictError(
                    "Meeting State changed during extraction"
                )

            candidates = CandidateMergePolicy(db_session).apply(
                session_id=session_id,
                operations=delta.action_operations,
                evidence_by_segment={value.segment_id: value for value in batch},
            )
            next_state = merge_meeting_state(
                current,
                delta,
                action_candidates=candidates,
                source_segment_revisions={
                    segment.segment_id: segment.revision for segment in batch
                },
                next_version=expected_version + 1,
            )
            for segment in batch:
                repository.advance_offset(
                    session_id=session_id,
                    segment_id=segment.segment_id,
                    processed_revision=segment.revision,
                    processed_at=now,
                )

            pending_count = self._pending_count(db_session, session_id, frontier=admission.terminal_frontier)
            latest_final_updated_at = db_session.scalar(
                select(func.max(SegmentRecord.updated_at)).where(
                    SegmentRecord.session_id == session_id,
                    SegmentRecord.status == "final",
                )
            )
            batch_frontier = max(
                (segment.updated_at for segment in batch),
                key=_aware,
            )
            projected_through = _later(
                live_head.projected_through if live_head is not None else None,
                batch_frontier,
            )
            lag_ms = 0
            if latest_final_updated_at is not None and projected_through is not None:
                lag_ms = max(
                    0,
                    int(
                        (
                            _aware(latest_final_updated_at)
                            - _aware(projected_through)
                        ).total_seconds()
                        * 1_000
                    ),
                )
            status = "ready" if pending_count == 0 else "lagging"
            if (
                pending_count > 0
                and lag_ms >= self._config.stale_after_seconds * 1_000
            ):
                status = "stale"
            frontier = self._source_frontier(db_session, session_id)
            repository.replace_head(
                session_id=session_id,
                expected_version=expected_version,
                status=status,
                state=next_state,
                state_hash=canonical_state_hash(next_state),
                source_frontier=frontier,
                latest_final_updated_at=latest_final_updated_at,
                projected_through=projected_through,
                lag_ms=lag_ms,
                pending_segment_count=pending_count,
                last_success_at=now,
            )
            db_session.commit()

    @staticmethod
    def _pending_count(db_session, session_id: str, *, frontier: Mapping[str, int] | None = None) -> int:
        if frontier is not None:
            offsets = dict(db_session.execute(select(MeetingProjectionOffsetRecord.segment_id, MeetingProjectionOffsetRecord.processed_revision).where(MeetingProjectionOffsetRecord.session_id == session_id)).all())
            return sum(offsets.get(key, 0) < revision for key, revision in frontier.items())
        offset_match = (
            (MeetingProjectionOffsetRecord.session_id == SegmentRecord.session_id)
            & (MeetingProjectionOffsetRecord.segment_id == SegmentRecord.segment_id)
        )
        return int(
            db_session.scalar(
                select(func.count(SegmentRecord.id))
                .outerjoin(MeetingProjectionOffsetRecord, offset_match)
                .where(
                    SegmentRecord.session_id == session_id,
                    SegmentRecord.status == "final",
                    or_(
                        MeetingProjectionOffsetRecord.processed_revision.is_(None),
                        MeetingProjectionOffsetRecord.processed_revision
                        < SegmentRecord.revision,
                    ),
                )
            )
            or 0
        )

    @staticmethod
    def _source_frontier(db_session, session_id: str) -> dict[str, object]:
        offsets = list(
            db_session.scalars(
                select(MeetingProjectionOffsetRecord)
                .where(MeetingProjectionOffsetRecord.session_id == session_id)
                .order_by(MeetingProjectionOffsetRecord.segment_id)
            )
        )
        return {
            "processed_revisions": {
                value.segment_id: value.processed_revision for value in offsets
            },
            "processed_segment_count": len(offsets),
            "updated_at": (
                max((value.processed_at for value in offsets), key=_aware).isoformat()
                if offsets
                else None
            ),
        }

    async def _acknowledge(
        self,
        session_id: str,
        batch: tuple[ProjectionSegment, ...],
        *,
        analysis_epoch: int,
    ) -> None:
        async with self._lock:
            buffer = self._buffers.get(session_id)
            if buffer is None or buffer.analysis_epoch != analysis_epoch:
                return
            for segment in batch:
                current = buffer.segments.get(segment.segment_id)
                if current is not None and current.revision <= segment.revision:
                    del buffer.segments[segment.segment_id]
            buffer.failure_count = 0
            buffer.retry_at = 0
            if not buffer.segments:
                buffer.first_pending_at = None
                buffer.priority_requested = False
            else:
                buffer.first_pending_at = asyncio.get_running_loop().time()

    async def _record_failure(self, session_id: str, error: Exception, *, analysis_epoch: int) -> None:
        async with self._lock:
            buffer = self._buffers.get(session_id)
            if buffer is None or buffer.analysis_epoch != analysis_epoch:
                return
            delay_index = min(
                buffer.failure_count,
                len(self._config.retry_delays_seconds) - 1,
            )
            delay = self._config.retry_delays_seconds[delay_index]
            buffer.failure_count += 1
            buffer.retry_at = asyncio.get_running_loop().time() + delay
        try:
            now = utc_now()
            with self._database.session() as db_session:
                admission = self._policy.fence(db_session, session_id, analysis_epoch)
                pending_count = self._pending_count(db_session, session_id, frontier=admission.terminal_frontier)
                latest_final_updated_at, oldest_final_updated_at = db_session.execute(
                    select(
                        func.max(SegmentRecord.updated_at),
                        func.min(SegmentRecord.updated_at),
                    ).where(
                        SegmentRecord.session_id == session_id,
                        SegmentRecord.status == "final",
                    )
                ).one()
                head = MeetingStateRepository(db_session).get_head(session_id)
                lag_from = (
                    head.projected_through
                    if head is not None and head.projected_through is not None
                    else oldest_final_updated_at
                )
                lag_ms = 0
                if lag_from is not None:
                    lag_ms = max(
                        0,
                        int((_aware(now) - _aware(lag_from)).total_seconds() * 1_000),
                    )
                initial_state = MeetingState(session_id=session_id, version=0)
                MeetingStateRepository(db_session).record_projection_error(
                    session_id=session_id,
                    error_code=f"projection_{type(error).__name__}"[:128],
                    initial_state=initial_state,
                    initial_state_hash=canonical_state_hash(initial_state),
                    latest_final_updated_at=latest_final_updated_at,
                    lag_ms=lag_ms,
                    pending_segment_count=pending_count,
                    failed_at=now,
                )
                db_session.commit()
        except MeetingPluginDenied:
            return
        except Exception as persistence_error:
            logger.error(
                "Meeting State projection failure metadata could not be persisted",
                extra={
                    "event": "meeting_state_projection_failure_persist_failed",
                    "session_id": session_id,
                    "internal_error_type": type(persistence_error).__name__,
                },
            )
        logger.error(
            "Meeting State projection failed",
            extra={
                "event": "meeting_state_projection_failed",
                "session_id": session_id,
                "retry_delay_seconds": delay,
                "internal_error_type": type(error).__name__,
            },
        )

    def _resolve_waiters(self, session_id: str) -> None:
        targets = self._waiters.get(session_id)
        if not targets:
            return
        with self._database.session() as db_session:
            offsets = {
                value.segment_id: value.processed_revision
                for value in db_session.scalars(
                    select(MeetingProjectionOffsetRecord).where(
                        MeetingProjectionOffsetRecord.session_id == session_id
                    )
                )
            }
            head = db_session.get(MeetingStateHeadRecord, session_id)
            state_version = head.version if head is not None else 0
            source_frontier = (
                dict(head.source_frontier_json) if head is not None else {}
            )
        pending: list[CatchUpTarget] = []
        admission = self._policy.admission(session_id)
        for target in targets:
            # A new activation may process the same revisions, but it cannot
            # fulfill a catch-up request authorized by a revoked generation.
            if admission.analysis_epoch != target.analysis_epoch:
                target._future.cancel()
                continue
            reached = all(
                offsets.get(segment_id, 0) >= revision
                for segment_id, revision in target.target_revisions.items()
            )
            if reached and not target._future.done():
                target._future.set_result(
                    CatchUpResult(
                        session_id=session_id,
                        state_version=state_version,
                        source_frontier=MappingProxyType(source_frontier),
                    )
                )
            elif not admission.permits(target.analysis_epoch):
                target._future.cancel()
            elif not target._future.done():
                pending.append(target)
        if pending:
            self._waiters[session_id] = pending
        else:
            self._waiters.pop(session_id, None)
