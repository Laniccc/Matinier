from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from livekit import api
from sqlalchemy import select

from app.errors import LiveKitOperationError
from app.hls.decoder import (
    FFmpegHLSDecoder,
    HLSDecodeError,
    HLSNoAudioError,
    HLSStartTimeoutError,
)
from app.hls.source import HLSLiveSource
from app.persistence.database import Database
from app.persistence.models import SessionRecord
from app.persistence.sessions import SessionRepository
from app.sessions.state import TERMINAL_SESSION_STATES
from app.settings import Settings


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HLSStartRequest:
    session_id: str
    room_id: str
    room_name: str
    fetch_url: str


@dataclass(frozen=True)
class HLSCleanupResult:
    session_id: str
    outcome: Literal["stopped", "already_stopped", "not_found"]
    cleanup_status: Literal["completed", "failed"]
    detail: str

    @property
    def released(self) -> bool:
        return self.outcome != "not_found"


class ManagedHLSSource(Protocol):
    async def run(
        self,
        livekit_url: str,
        token: str,
        *,
        on_source_failed: Callable[[BaseException], Awaitable[None]],
    ) -> None: ...

    async def aclose(self) -> None: ...


TokenFactory = Callable[[HLSStartRequest], str]
SourceFactory = Callable[[HLSStartRequest], ManagedHLSSource]
FailureHandler = Callable[[HLSStartRequest, BaseException], Awaitable[None]]
OrphanReconciler = Callable[[], Awaitable[int]]
LifecycleHandler = Callable[
    [HLSStartRequest, str, str | None],
    Awaitable[None],
]
HLSInputManagerFactory = Callable[[Settings, Database], "HLSInputManager"]


async def _ignore_failure(
    _request: HLSStartRequest,
    _error: BaseException,
) -> None:
    return None


async def _no_orphans() -> int:
    return 0


async def _ignore_lifecycle(
    _request: HLSStartRequest,
    _status: str,
    _detail: str | None,
) -> None:
    return None


class HLSInputManager:
    """Own and stop server-side HLS publishers by caption Session."""

    def __init__(
        self,
        *,
        livekit_url: str,
        token_factory: TokenFactory,
        source_factory: SourceFactory,
        failure_handler: FailureHandler = _ignore_failure,
        orphan_reconciler: OrphanReconciler = _no_orphans,
        lifecycle_handler: LifecycleHandler = _ignore_lifecycle,
        stop_timeout_seconds: float = 5.0,
        retained_result_count: int = 256,
    ) -> None:
        if stop_timeout_seconds <= 0:
            raise ValueError("HLS stop timeout must be positive")
        if retained_result_count <= 0:
            raise ValueError("retained_result_count must be positive")
        self._livekit_url = livekit_url
        self._token_factory = token_factory
        self._source_factory = source_factory
        self._failure_handler = failure_handler
        self._orphan_reconciler = orphan_reconciler
        self._lifecycle_handler = lifecycle_handler
        self._stop_timeout_seconds = stop_timeout_seconds
        self._retained_result_count = retained_result_count
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._sources: dict[str, ManagedHLSSource] = {}
        self._requests: dict[str, HLSStartRequest] = {}
        self._stopping: set[str] = set()
        self._stop_locks: dict[str, asyncio.Lock] = {}
        self._completed: OrderedDict[str, HLSCleanupResult] = OrderedDict()
        self._started_at_ms: dict[str, float] = {}

    @property
    def active_session_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._tasks))

    async def start(self, request: HLSStartRequest) -> None:
        if request.session_id in self._tasks:
            raise RuntimeError("HLS input is already running")
        source = self._source_factory(request)
        token = self._token_factory(request)
        self._sources[request.session_id] = source
        self._requests[request.session_id] = request
        self._started_at_ms[request.session_id] = time.monotonic() * 1_000
        self._completed.pop(request.session_id, None)
        await self._set_lifecycle(request, "starting")
        logger.info(
            "HLS source started",
            extra={
                "process_name": "api",
                "session_id": request.session_id,
                "room_name": request.room_name,
                "participant_identity": f"hls-{request.session_id}",
                "source_type": "hls",
                "event": "source_started",
                "status": "starting",
                "elapsed_ms": self._elapsed_ms(request.session_id),
            },
        )
        task = asyncio.create_task(
            self._run(request, source, token),
            name=f"hls-input-{request.session_id}",
        )
        self._tasks[request.session_id] = task
        await asyncio.sleep(0)
        if request.session_id in self._tasks:
            await self._set_lifecycle(request, "running")

    async def stop(
        self,
        session_id: str,
        *,
        graceful: bool,
        force: bool = False,
    ) -> HLSCleanupResult:
        del graceful  # Reserved for a future drain policy; cleanup is always bounded.
        lock = self._stop_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            task = self._tasks.get(session_id)
            source = self._sources.get(session_id)
            request = self._requests.get(session_id)
            if task is None or source is None or request is None:
                previous = self._completed.get(session_id)
                if previous is not None:
                    return HLSCleanupResult(
                        session_id=session_id,
                        outcome="already_stopped",
                        cleanup_status=previous.cleanup_status,
                        detail=previous.detail,
                    )
                return HLSCleanupResult(
                    session_id=session_id,
                    outcome="not_found",
                    cleanup_status="completed" if force else "failed",
                    detail="No API-owned HLS source is registered.",
                )

            self._stopping.add(session_id)
            await self._set_lifecycle(request, "stopping")
            close_error: BaseException | None = None
            try:
                try:
                    await source.aclose()
                except BaseException as error:
                    if isinstance(error, asyncio.CancelledError):
                        raise
                    close_error = error
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=self._stop_timeout_seconds,
                    )
                except TimeoutError:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            finally:
                self._remove(session_id, task)
                self._stopping.discard(session_id)

            result = self._completed.get(session_id)
            if result is None:
                result = HLSCleanupResult(
                    session_id=session_id,
                    outcome="stopped",
                    cleanup_status="completed",
                    detail="Source task stopped and ownership was released.",
                )
            if close_error is not None:
                result = HLSCleanupResult(
                    session_id=session_id,
                    outcome="stopped",
                    cleanup_status="failed",
                    detail=(
                        "Source close raised "
                        f"{type(close_error).__name__}; remaining cleanup continued."
                    ),
                )
                await self._set_lifecycle(request, "failed", result.detail)
            self._remember_result(result)
            return result

    async def stop_for_room(
        self,
        room_id: str,
        *,
        graceful: bool,
    ) -> None:
        session_ids = tuple(
            session_id
            for session_id, request in self._requests.items()
            if request.room_id == room_id
        )
        for session_id in session_ids:
            await self.stop(session_id, graceful=graceful)

    async def aclose(self) -> None:
        for session_id in tuple(self._tasks):
            await self.stop(session_id, graceful=False)

    async def reconcile_orphans(self) -> int:
        return await self._orphan_reconciler()

    def runtime_snapshot(self, session_id: str) -> dict[str, object] | None:
        source = self._sources.get(session_id)
        snapshot_factory = getattr(source, "runtime_snapshot", None)
        if callable(snapshot_factory):
            return dict(snapshot_factory())
        completed = self._completed.get(session_id)
        if completed is not None:
            return {
                "room_connected": False,
                "publisher_connected": False,
                "track_published": False,
                "ffmpeg_running": False,
            }
        return None

    async def _run(
        self,
        request: HLSStartRequest,
        source: ManagedHLSSource,
        token: str,
    ) -> None:
        failure_reported = False
        failure_code: str | None = None

        async def report_failure(error: BaseException) -> None:
            nonlocal failure_reported
            if (
                failure_reported
                or request.session_id in self._stopping
            ):
                return
            failure_reported = True
            await self._failure_handler(request, error)

        try:
            await source.run(
                self._livekit_url,
                token,
                on_source_failed=report_failure,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            await report_failure(error)
            failure_code, _ = _failure_details(error)
            if request.session_id not in self._stopping:
                failure_fields = {
                    "process_name": "api",
                    "session_id": request.session_id,
                    "room_name": request.room_name,
                    "participant_identity": f"hls-{request.session_id}",
                    "source_type": "hls",
                    "status": "failed",
                    "failure_code": failure_code,
                    "cleanup_status": "in_progress",
                    "elapsed_ms": self._elapsed_ms(request.session_id),
                    "internal_error_type": type(error).__name__,
                }
                logger.exception(
                    "HLS input task failed",
                    extra={**failure_fields, "event": "source_failed"},
                )
                logger.error(
                    "caption session failed after HLS source failure",
                    extra={**failure_fields, "event": "session_failed"},
                )
        finally:
            cleanup_errors = tuple(
                getattr(source, "cleanup_errors", ())
            )
            if cleanup_errors:
                result = HLSCleanupResult(
                    session_id=request.session_id,
                    outcome="stopped",
                    cleanup_status="failed",
                    detail="; ".join(cleanup_errors),
                )
                await self._set_lifecycle(request, "failed", result.detail)
            else:
                detail = str(
                    getattr(
                        source,
                        "cleanup_detail",
                        "Source task stopped and ownership was released.",
                    )
                )
                result = HLSCleanupResult(
                    session_id=request.session_id,
                    outcome="stopped",
                    cleanup_status="completed",
                    detail=detail,
                )
                await self._set_lifecycle(request, "stopped", detail)
            logger.info(
                "HLS source stopped",
                extra={
                    "process_name": "api",
                    "session_id": request.session_id,
                    "room_name": request.room_name,
                    "participant_identity": f"hls-{request.session_id}",
                    "source_type": "hls",
                    "event": "source_stopped",
                    "status": "stopped",
                    "failure_code": failure_code,
                    "cleanup_status": result.cleanup_status,
                    "elapsed_ms": self._elapsed_ms(request.session_id),
                },
            )
            self._remember_result(result)
            self._remove(request.session_id, asyncio.current_task())

    def _elapsed_ms(self, session_id: str) -> float | None:
        started_at = self._started_at_ms.get(session_id)
        if started_at is None:
            return None
        return round(time.monotonic() * 1_000 - started_at, 3)

    def _remove(
        self,
        session_id: str,
        task: asyncio.Task[Any] | None,
    ) -> None:
        current = self._tasks.get(session_id)
        if task is not None and current is not task:
            return
        self._tasks.pop(session_id, None)
        self._sources.pop(session_id, None)
        self._requests.pop(session_id, None)
        self._started_at_ms.pop(session_id, None)

    def _remember_result(self, result: HLSCleanupResult) -> None:
        self._completed[result.session_id] = result
        self._completed.move_to_end(result.session_id)
        while len(self._completed) > self._retained_result_count:
            self._completed.popitem(last=False)

    async def _set_lifecycle(
        self,
        request: HLSStartRequest,
        status: str,
        detail: str | None = None,
    ) -> None:
        try:
            await self._lifecycle_handler(request, status, detail)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            logger.error(
                "HLS lifecycle persistence failed",
                extra={
                    "process_name": "api",
                    "session_id": request.session_id,
                    "room_name": request.room_name,
                    "event": "hls_lifecycle_persistence_failed",
                    "status": status,
                    "internal_error_type": type(error).__name__,
                },
            )


def _failure_details(error: BaseException) -> tuple[str, str]:
    if isinstance(error, HLSNoAudioError):
        return "hls_no_audio", "The HLS playlist did not provide an audio stream."
    if isinstance(error, HLSStartTimeoutError):
        return "hls_start_timeout", "The HLS stream did not start in time."
    if isinstance(error, HLSDecodeError):
        return "hls_stream_error", "The HLS audio stream stopped unexpectedly."
    if isinstance(error, LiveKitOperationError):
        return "livekit_error", "The realtime media connection failed."
    return "hls_stream_error", "The HLS audio stream stopped unexpectedly."


def build_hls_input_manager(
    settings: Settings,
    database: Database,
) -> HLSInputManager:
    def token_factory(request: HLSStartRequest) -> str:
        grants = api.VideoGrants(
            room_join=True,
            room=request.room_name,
            can_publish=True,
            can_subscribe=False,
            can_publish_data=False,
            can_publish_sources=["microphone"],
        )
        return (
            api.AccessToken(
                settings.livekit_api_key,
                settings.livekit_api_secret,
            )
            .with_identity(f"hls-{request.session_id}")
            .with_name("M3U8 audio input")
            .with_ttl(dt.timedelta(hours=4))
            .with_grants(grants)
            .to_jwt()
        )

    def source_factory(request: HLSStartRequest) -> HLSLiveSource:
        decoder = FFmpegHLSDecoder(
            request.fetch_url,
            ffmpeg_path=settings.ffmpeg_bin,
            first_frame_timeout_seconds=(
                settings.hls_first_frame_timeout_seconds
            ),
            read_timeout_seconds=(
                settings.remote_media_read_timeout_seconds
            ),
            stop_timeout_seconds=settings.hls_stop_timeout_seconds,
            max_duration_seconds=(
                settings.max_remote_stream_duration_seconds
            ),
        )
        return HLSLiveSource(
            session_id=request.session_id,
            decoder=decoder,
            room_name=request.room_name,
            connect_timeout_seconds=(
                settings.livekit_connect_timeout_seconds
            ),
            subscription_timeout_seconds=(
                settings.livekit_connect_timeout_seconds
            ),
        )

    async def failure_handler(
        request: HLSStartRequest,
        error: BaseException,
    ) -> None:
        error_code, message = _failure_details(error)
        with database.session() as db_session:
            repository = SessionRepository(db_session)
            record = repository.get(request.session_id)
            if (
                record is None
                or record.status in TERMINAL_SESSION_STATES
            ):
                return
            repository.fail(
                request.session_id,
                error_code=error_code,
                error_message=message,
            )
            db_session.commit()

    async def lifecycle_handler(
        request: HLSStartRequest,
        source_status: str,
        detail: str | None,
    ) -> None:
        with database.session() as db_session:
            repository = SessionRepository(db_session)
            if repository.get(request.session_id) is None:
                return
            if source_status == "running":
                repository.mark_source_running(request.session_id)
            elif source_status == "stopping":
                repository.mark_source_stopping(request.session_id)
            elif source_status == "stopped":
                repository.mark_source_stopped(
                    request.session_id,
                    cleanup_detail=detail,
                )
            elif source_status == "failed":
                repository.mark_source_cleanup_failed(
                    request.session_id,
                    cleanup_detail=detail or "HLS cleanup failed.",
                )
            else:
                record = repository.get_required(request.session_id)
                record.source_status = "starting"
                record.cleanup_status = "not_started"
                record.cleanup_detail = None
                db_session.flush()
            db_session.commit()

    async def orphan_reconciler() -> int:
        with database.session() as db_session:
            records = list(
                db_session.scalars(
                    select(SessionRecord).where(
                        SessionRecord.source_type == "hls",
                        SessionRecord.status.not_in(
                            tuple(TERMINAL_SESSION_STATES)
                        ),
                    )
                )
            )
            repository = SessionRepository(db_session)
            for record in records:
                repository.fail(
                    record.id,
                    error_code="hls_process_lost",
                    error_message=(
                        "The API restarted while the HLS input was active."
                    ),
                )
                repository.mark_source_lost(
                    record.id,
                    cleanup_detail=(
                        "API restarted and no owned HLS process remained."
                    ),
                )
            if records:
                db_session.commit()
            return len(records)

    return HLSInputManager(
        livekit_url=settings.livekit_url,
        token_factory=token_factory,
        source_factory=source_factory,
        failure_handler=failure_handler,
        orphan_reconciler=orphan_reconciler,
        lifecycle_handler=lifecycle_handler,
        stop_timeout_seconds=settings.hls_stop_timeout_seconds,
    )
