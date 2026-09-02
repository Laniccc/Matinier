from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from livekit import agents, rtc

from app.errors import LiveKitOperationError, error_category
from app.captions.runtime import create_worker_caption_runtime
from app.logging import configure_logging
from app.settings import configure_livekit_jwt_warnings, get_settings
from app.transcription.bailian import (
    BailianConfig,
    BailianSpeechRecognitionProvider,
)
from app.transcription.fake import FakeASRConfig, FakeASRProvider
from app.transcription.provider import SpeechRecognitionProvider
from app.transcription.session import EventHandler, TranscriptionSession
from app.translation.bailian import (
    BailianLiveTranslateConfig,
    BailianLiveTranslateProvider,
)
from app.translation.fake import (
    FakeTranslationConfig,
    FakeTranslationProvider,
)
from app.translation.provider import SpeechTranslationProvider
from app.translation.runtime import (
    WorkerTranslationRuntime,
    create_worker_translation_runtime,
)
from app.translation.session import (
    EventHandler as TranslationEventHandler,
    TranslationSession,
)
from app.worker.audio_router import AudioRouter
from app.worker.audio_stats import (
    AudioFrameStats,
    resolve_caption_track_target,
)
from app.worker.control import publish_runtime_snapshot, request_source_abort
from app.worker.modes import WorkerRuntimeMode
from app.worker.transport_runtime import (
    TransportFrameSink,
    consume_transport_audio,
)
from app.worker.health import WorkerHealthStore


settings = get_settings()
configure_livekit_jwt_warnings(settings)
configure_logging("worker", settings.log_level)
logger = logging.getLogger(__name__)
worker_health_store = WorkerHealthStore(settings.worker_health_dir)
logger.info(
    "worker storage configured",
    extra={
        "process_name": "worker",
        "event": "worker_storage_configured",
        "database_path": settings.database_location,
        "worker_runtime_mode": settings.worker_runtime_mode.value,
    },
)

# The LiveKit Agents CLI reads credentials from the process environment.
os.environ.setdefault("LIVEKIT_URL", settings.livekit_url)
os.environ.setdefault("LIVEKIT_API_KEY", settings.livekit_api_key)
os.environ.setdefault("LIVEKIT_API_SECRET", settings.livekit_api_secret)


def build_transcription_session(
    *,
    session_id: str,
    room_name: str,
    participant_identity: str,
    language: str = "auto",
    event_handler: EventHandler | None = None,
) -> TranscriptionSession:
    def provider_factory() -> SpeechRecognitionProvider:
        if settings.worker_runtime_mode is WorkerRuntimeMode.FAKE_PROVIDER:
            return FakeASRProvider(
                FakeASRConfig(
                    chunks_per_segment=(
                        settings.fake_provider_chunks_per_segment
                    ),
                    send_delay_seconds=(
                        settings.fake_provider_send_delay_ms / 1_000
                    ),
                    emit_duplicate_final=(
                        settings.fake_provider_duplicate_final
                    ),
                    emit_stale_partial=(
                        settings.fake_provider_stale_partial
                    ),
                    fail_after_chunks=settings.fake_asr_fail_after_chunks,
                    timeout_on_finish=settings.fake_asr_timeout_on_finish,
                )
            )
        return BailianSpeechRecognitionProvider(
            BailianConfig(
                api_key=settings.dashscope_api_key,
                workspace_id=settings.dashscope_workspace_id,
                region=settings.dashscope_region,
                model=settings.bailian_asr_model,
                websocket_url=settings.dashscope_websocket_url,
                proxy_url=settings.dashscope_websocket_proxy_url,
                start_timeout_seconds=settings.asr_start_timeout_seconds,
                finish_timeout_seconds=settings.asr_finish_timeout_seconds,
                language=language,
            )
        )

    return TranscriptionSession(
        provider_factory=provider_factory,
        queue_max_chunks=settings.asr_queue_max_chunks,
        chunk_duration_ms=settings.asr_chunk_duration_ms,
        startup_retries=settings.asr_startup_retries,
        event_handler=event_handler,
        log_context={
            "session_id": session_id,
            "room_name": room_name,
            "participant_identity": participant_identity,
        },
        log_provider_payloads=settings.log_provider_payloads,
    )


def build_translation_session(
    *,
    source_language: str,
    target_language: str,
    event_handler: TranslationEventHandler | None = None,
) -> TranslationSession:
    def provider_factory() -> SpeechTranslationProvider:
        if settings.worker_runtime_mode is WorkerRuntimeMode.FAKE_PROVIDER:
            return FakeTranslationProvider(
                FakeTranslationConfig(
                    target_language=target_language,
                    chunks_per_segment=(
                        settings.fake_provider_chunks_per_segment
                    ),
                    send_delay_seconds=(
                        settings.fake_provider_send_delay_ms / 1_000
                    ),
                    emit_duplicate_final=(
                        settings.fake_provider_duplicate_final
                    ),
                    emit_stale_partial=(
                        settings.fake_provider_stale_partial
                    ),
                    fail_after_chunks=(
                        settings.fake_translation_fail_after_chunks
                    ),
                    timeout_on_finish=(
                        settings.fake_translation_timeout_on_finish
                    ),
                )
            )
        return BailianLiveTranslateProvider(
            BailianLiveTranslateConfig(
                api_key=settings.dashscope_api_key,
                workspace_id=settings.dashscope_workspace_id,
                region=settings.dashscope_region,
                model=settings.bailian_translation_model,
                websocket_url=settings.dashscope_translation_websocket_url,
                proxy_url=settings.dashscope_websocket_proxy_url,
                source_language=source_language,
                target_language=target_language,
                start_timeout_seconds=settings.translation_start_timeout_seconds,
                finish_timeout_seconds=settings.translation_finish_timeout_seconds,
            )
        )

    return TranslationSession(
        provider_factory=provider_factory,
        queue_max_chunks=settings.translation_queue_max_chunks,
        chunk_duration_ms=settings.asr_chunk_duration_ms,
        event_handler=event_handler,
    )


async def drain_track_tasks(
    tasks: tuple[asyncio.Task[Any], ...],
    *,
    timeout_seconds: float,
) -> None:
    if not tasks:
        return
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    _, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
    for task in pending:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def iter_audio_events_until_stop(
    stream: Any,
    stop_event: asyncio.Event | None,
) -> AsyncIterator[Any]:
    if stop_event is None:
        async for event in stream:
            yield event
        return

    iterator = stream.__aiter__()
    stop_task = asyncio.create_task(
        stop_event.wait(),
        name="replay-audio-stop-waiter",
    )
    frame_task: asyncio.Task[Any] | None = None
    try:
        while True:
            frame_task = asyncio.create_task(
                anext(iterator),
                name="replay-audio-next-frame",
            )
            # Once the participant has left, still allow an already-buffered
            # AudioStream frame to win before treating the stream as ended.
            if stop_task.done():
                await asyncio.sleep(0)
            done, _ = await asyncio.wait(
                (frame_task, stop_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if frame_task in done:
                try:
                    event = frame_task.result()
                except StopAsyncIteration:
                    return
                frame_task = None
                yield event
                continue

            frame_task.cancel()
            await asyncio.gather(frame_task, return_exceptions=True)
            frame_task = None
            return
    finally:
        if frame_task is not None and not frame_task.done():
            frame_task.cancel()
            await asyncio.gather(frame_task, return_exceptions=True)
        if not stop_task.done():
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)


async def consume_replay_audio(
    *,
    track: Any,
    room_name: str,
    participant_identity: str,
    session_id: str | None = None,
    stop_event: asyncio.Event | None = None,
    stream_factory: Callable[..., Any] = rtc.AudioStream,
    transcription_session_factory: Callable[..., Any] = build_transcription_session,
    caption_runtime: Any | None = None,
    translation_session_factory: Callable[..., Any] = build_translation_session,
    translation_runtime: WorkerTranslationRuntime | None = None,
    source_abort_handler: Callable[[str, str], Awaitable[None]] | None = None,
    runtime_snapshot_handler: Callable[
        [dict[str, object]], Awaitable[None]
    ] | None = None,
    track_sid: str | None = None,
) -> AudioFrameStats:
    resolved_session_id = (
        session_id or participant_identity.removeprefix("replay-")
    )
    stats = AudioFrameStats()
    stream = stream_factory(
        track,
        sample_rate=16_000,
        num_channels=1,
        frame_size_ms=20,
    )
    transcription_session: Any | None = None
    translation_session: Any | None = None
    audio_router: AudioRouter | None = None
    error_type: str | None = None
    asr_connected = False
    translation_connected = False
    started_at_ms = time.monotonic() * 1_000

    async def report_runtime(
        *,
        room_connected: bool = True,
        track_subscribed: bool = True,
    ) -> None:
        if runtime_snapshot_handler is None:
            return
        queue_size = 0
        queue_max = 0
        if transcription_session is not None:
            queue_size = int(getattr(transcription_session, "queue_size", 0))
            queue_max = int(
                getattr(transcription_session, "max_queue_size", 0)
            )
        await runtime_snapshot_handler(
            {
                "room_connected": room_connected,
                "track_subscribed": track_subscribed,
                "asr_connected": asr_connected,
                "translation_connected": translation_connected,
                "audio_bytes": stats.audio_bytes,
                "audio_frames": stats.frame_count,
                "audio_queue_current": queue_size,
                "audio_queue_max": queue_max,
                "last_event_at": dt.datetime.now(dt.UTC).isoformat(),
            }
        )

    async def degrade_translation(error: BaseException) -> None:
        if translation_runtime is None:
            return
        try:
            await translation_runtime.fail(error)
        except asyncio.CancelledError:
            raise
        except BaseException as report_error:
            logger.error(
                "translation degradation reporting failed",
                extra={
                    "process_name": "worker",
                    "session_id": resolved_session_id,
                    "room_name": room_name,
                    "participant_identity": participant_identity,
                    "event": "translation_failure_reporting_failed",
                    "internal_error_type": type(report_error).__name__,
                },
            )

    async def close_translation_session() -> None:
        if translation_session is None:
            return
        try:
            await translation_session.aclose()
        except asyncio.CancelledError:
            raise
        except BaseException as close_error:
            logger.error(
                "translation session cleanup failed",
                extra={
                    "process_name": "worker",
                    "session_id": resolved_session_id,
                    "room_name": room_name,
                    "participant_identity": participant_identity,
                    "event": "translation_cleanup_failed",
                    "internal_error_type": type(close_error).__name__,
                },
            )
    try:
        transcription_arguments: dict[str, Any] = {
            "session_id": resolved_session_id,
            "room_name": room_name,
            "participant_identity": participant_identity,
            "event_handler": (
                caption_runtime.handle_asr_event
                if caption_runtime is not None
                else None
            ),
        }
        source_language = getattr(caption_runtime, "source_language", None)
        if isinstance(source_language, str) and source_language:
            transcription_arguments["language"] = source_language
        transcription_session = transcription_session_factory(
            **transcription_arguments
        )
        if caption_runtime is not None:
            await caption_runtime.start_replay()
        await transcription_session.start()
        asr_connected = True
        await report_runtime()
        audio_router = AudioRouter(transcription_session)
        if translation_runtime is not None:
            try:
                await translation_runtime.start()
                translation_session = translation_session_factory(
                    source_language=translation_runtime.source_language,
                    target_language=translation_runtime.target_language,
                    event_handler=translation_runtime.handle_event,
                )
                await translation_session.start()
                translation_connected = True
                await report_runtime()

                async def translation_route_failed(
                    error: BaseException,
                ) -> None:
                    await degrade_translation(error)
                    await close_translation_session()
                    nonlocal translation_connected
                    translation_connected = False

                audio_router.add_optional(
                    "translation",
                    translation_session,
                    on_failure=translation_route_failed,
                )
            except asyncio.CancelledError:
                raise
            except BaseException as translation_error:
                await degrade_translation(translation_error)
                await close_translation_session()
                translation_session = None
                translation_connected = False
        async for event in iter_audio_events_until_stop(stream, stop_event):
            stats.observe(event.frame)
            assert audio_router is not None
            await audio_router.route(bytes(event.frame.data))
            # AudioStream may drain frames buffered while the provider was starting
            # without suspending between __anext__ calls. Yield so the bounded ASR
            # sender can make progress before the next aggregate chunk is enqueued.
            await asyncio.sleep(0)
            if stats.frame_count == 1 or stats.frame_count % 25 == 0:
                if caption_runtime is not None and stats.frame_count % 25 == 0:
                    await caption_runtime.publish_progress(
                        int(round(stats.audio_duration_seconds * 1_000))
                    )
                logger.info(
                    "replay audio progress",
                    extra={
                        "process_name": "worker",
                        "session_id": resolved_session_id,
                        "room_name": room_name,
                        "participant_identity": participant_identity,
                        "event": "replay_audio_progress",
                        **stats.log_fields(),
                    },
                )
            if stats.frame_count == 1 or stats.frame_count % 250 == 0:
                await report_runtime()
        if caption_runtime is not None:
            await caption_runtime.begin_finalizing()
        metrics = await transcription_session.finish()
        asr_connected = False
        await report_runtime()
        if caption_runtime is not None:
            await caption_runtime.complete(metrics)
        if (
            translation_session is not None
            and translation_runtime is not None
            and audio_router is not None
            and audio_router.is_optional_active("translation")
        ):
            try:
                await translation_runtime.begin_finalizing()
                await translation_session.finish()
                translation_connected = False
                await translation_runtime.complete()
                await report_runtime()
            except asyncio.CancelledError:
                raise
            except BaseException as translation_error:
                await degrade_translation(translation_error)
    except BaseException as error:
        error_type = error_category(error, default="asr_stream_error")
        asr_connected = False
        translation_connected = False
        await report_runtime()
        if caption_runtime is not None:
            try:
                if isinstance(error, asyncio.CancelledError):
                    await caption_runtime.cancel()
                else:
                    await caption_runtime.fail(error)
            except BaseException as report_error:
                logger.error(
                    "caption failure reporting failed",
                    extra={
                        "process_name": "worker",
                        "session_id": resolved_session_id,
                        "room_name": room_name,
                        "participant_identity": participant_identity,
                        "event": "caption_failure_reporting_failed",
                        "error_type": error_category(
                            report_error,
                            default="persistence_error",
                        ),
                        "internal_error_type": type(report_error).__name__,
                    },
                )
        if (
            not isinstance(error, asyncio.CancelledError)
            and translation_runtime is not None
            and translation_session is not None
        ):
            # The translation route is optional while source ASR is healthy,
            # but it cannot remain "running" after the authoritative source
            # route has failed. Persist its terminal state before asking the
            # API to tear down a remote source.
            await degrade_translation(error)
            await close_translation_session()
            translation_session = None
        if (
            source_abort_handler is not None
            and getattr(caption_runtime, "source_type", None) == "hls"
        ):
            if isinstance(error, asyncio.CancelledError):
                abort_reason = "worker_cancelled"
            elif error_type in {"asr_auth_error", "asr_stream_error"}:
                abort_reason = error_type
            else:
                abort_reason = "caption_runtime_error"
            try:
                await source_abort_handler(
                    abort_reason,
                    str(error) or type(error).__name__,
                )
            except BaseException as abort_error:
                logger.error(
                    "source abort request failed",
                    extra={
                        "process_name": "worker",
                        "session_id": resolved_session_id,
                        "room_name": room_name,
                        "participant_identity": participant_identity,
                        "event": "source_abort_failed",
                        "failure_code": abort_reason,
                        "cleanup_status": "pending",
                        "internal_error_type": type(abort_error).__name__,
                    },
                )
        raise
    finally:
        try:
            if transcription_session is not None:
                await transcription_session.aclose()
        finally:
            try:
                await close_translation_session()
            finally:
                try:
                    if translation_runtime is not None:
                        try:
                            await translation_runtime.aclose()
                        except asyncio.CancelledError:
                            raise
                        except BaseException as close_error:
                            logger.error(
                                "translation runtime cleanup failed",
                                extra={
                                    "process_name": "worker",
                                    "session_id": resolved_session_id,
                                    "room_name": room_name,
                                    "participant_identity": participant_identity,
                                    "event": "translation_runtime_cleanup_failed",
                                    "internal_error_type": type(close_error).__name__,
                                },
                            )
                finally:
                    try:
                        await stream.aclose()
                    except BaseException as cleanup_error:
                        if caption_runtime is not None:
                            await caption_runtime.fail_source_cleanup(
                                cleanup_error
                            )
                        raise
                    else:
                        if caption_runtime is not None:
                            await caption_runtime.complete_source_cleanup()
                    finally:
                        if caption_runtime is not None:
                            await caption_runtime.aclose()
        asr_connected = False
        translation_connected = False
        await report_runtime(track_subscribed=False)
        logger.info(
            "replay audio complete",
            extra={
                "process_name": "worker",
                "session_id": resolved_session_id,
                "room_name": room_name,
                "participant_identity": participant_identity,
                "track_sid": track_sid,
                "source_type": getattr(caption_runtime, "source_type", None),
                "event": "worker_cleanup_completed",
                "error_type": error_type,
                "failure_code": error_type,
                "cleanup_status": "completed",
                "status": "failed" if error_type is not None else "completed",
                "elapsed_ms": round(
                    time.monotonic() * 1_000 - started_at_ms,
                    3,
                ),
                **stats.log_fields(),
            },
        )
    return stats


async def worker_entrypoint(ctx: agents.JobContext) -> None:
    raw_job_id = getattr(getattr(ctx, "job", None), "id", None)
    job_health_id = str(raw_job_id or f"{os.getpid()}-{id(ctx)}")
    worker_health_store.mark_job_started(job_health_id)
    track_tasks: dict[str, asyncio.Task[Any]] = {}
    track_stops: dict[str, tuple[str, asyncio.Event]] = {}

    def on_participant_connected(participant: rtc.RemoteParticipant) -> None:
        logger.info(
            "room participant connected",
            extra={
                "process_name": "worker",
                "room_name": ctx.room.name,
                "participant_identity": participant.identity,
                "event": "room_participant_connected",
            },
        )

    def on_participant_disconnected(participant: rtc.RemoteParticipant) -> None:
        for identity, stop_event in tuple(track_stops.values()):
            if identity == participant.identity:
                stop_event.set()
        logger.info(
            "room participant disconnected",
            extra={
                "process_name": "worker",
                "room_name": ctx.room.name,
                "participant_identity": participant.identity,
                "event": "room_participant_disconnected",
            },
        )

    def on_track_published(
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if resolve_caption_track_target(participant, publication) is not None:
            publication.set_subscribed(True)

    def on_track_unpublished(
        publication: rtc.RemoteTrackPublication,
        _participant: rtc.RemoteParticipant,
    ) -> None:
        tracked = track_stops.get(publication.sid)
        if tracked is not None:
            tracked[1].set()

    def on_track_subscribed(
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        target = resolve_caption_track_target(participant, publication)
        if target is None:
            return
        if publication.sid in track_tasks:
            return

        stop_event = asyncio.Event()

        async def consume_track_with_captions() -> Any:
            runtime_mode = settings.worker_runtime_mode
            asr_model = (
                "frame-sink"
                if runtime_mode is WorkerRuntimeMode.TRANSPORT_ONLY
                else (
                    "fake-asr-v1"
                    if runtime_mode is WorkerRuntimeMode.FAKE_PROVIDER
                    else settings.bailian_asr_model
                )
            )
            caption_runtime = create_worker_caption_runtime(
                session_id=target.session_id,
                track_id=publication.sid,
                local_participant=ctx.room.local_participant,
                database_url=settings.database_url,
                provider_name=runtime_mode.provider_name,
                model_name=asr_model,
            )
            logger.info(
                "Worker subscribed to caption audio track",
                extra={
                    "process_name": "worker",
                    "session_id": target.session_id,
                    "room_name": ctx.room.name,
                    "participant_identity": participant.identity,
                    "track_sid": publication.sid,
                    "source_type": caption_runtime.source_type,
                    "provider": runtime_mode.provider_name,
                    "event": "worker_subscribed",
                    "status": "running",
                    "elapsed_ms": 0.0,
                },
            )

            async def abort_source(reason: str, detail: str) -> None:
                await request_source_abort(
                    settings=settings,
                    session_id=target.session_id,
                    reason=reason,
                    detail=detail,
                    room_name=ctx.room.name,
                    participant_identity=participant.identity,
                )

            async def report_snapshot(payload: dict[str, object]) -> None:
                try:
                    await publish_runtime_snapshot(
                        settings=settings,
                        session_id=target.session_id,
                        snapshot=payload,
                    )
                except asyncio.CancelledError:
                    raise
                except BaseException as report_error:
                    logger.warning(
                        "runtime snapshot reporting failed",
                        extra={
                            "process_name": "worker",
                            "session_id": target.session_id,
                            "room_name": ctx.room.name,
                            "participant_identity": participant.identity,
                            "event": "runtime_snapshot_failed",
                            "internal_error_type": type(report_error).__name__,
                        },
                    )

            if runtime_mode is WorkerRuntimeMode.TRANSPORT_ONLY:
                return await consume_transport_audio(
                    track=track,
                    room_name=ctx.room.name,
                    participant_identity=participant.identity,
                    session_id=target.session_id,
                    stop_event=stop_event,
                    sink_factory=lambda: TransportFrameSink(
                        queue_max_frames=settings.transport_queue_max_frames,
                        consumer_delay_seconds=(
                            settings.transport_consumer_delay_ms / 1_000
                        ),
                    ),
                    caption_runtime=caption_runtime,
                    source_abort_handler=abort_source,
                    track_sid=publication.sid,
                )

            translation_model = (
                "fake-translation-v1"
                if runtime_mode is WorkerRuntimeMode.FAKE_PROVIDER
                else settings.bailian_translation_model
            )
            try:
                translation_runtime = create_worker_translation_runtime(
                    session_id=target.session_id,
                    database_url=settings.database_url,
                    provider_name=runtime_mode.provider_name,
                    model_name=translation_model,
                    local_participant=ctx.room.local_participant,
                    log_context={
                        "room_name": ctx.room.name,
                        "participant_identity": participant.identity,
                        "track_sid": publication.sid,
                        "source_type": caption_runtime.source_type,
                    },
                )
            except BaseException as translation_error:
                logger.error(
                    "translation runtime could not be created",
                    extra={
                        "process_name": "worker",
                        "session_id": target.session_id,
                        "room_name": ctx.room.name,
                        "participant_identity": participant.identity,
                        "event": "translation_runtime_create_failed",
                        "internal_error_type": type(translation_error).__name__,
                    },
                )
                translation_runtime = None

            return await consume_replay_audio(
                track=track,
                room_name=ctx.room.name,
                participant_identity=participant.identity,
                session_id=target.session_id,
                stop_event=stop_event,
                caption_runtime=caption_runtime,
                translation_runtime=translation_runtime,
                source_abort_handler=abort_source,
                runtime_snapshot_handler=report_snapshot,
                track_sid=publication.sid,
            )

        task = asyncio.create_task(
            consume_track_with_captions(),
            name=f"caption-audio-{publication.sid}",
        )
        track_tasks[publication.sid] = task
        track_stops[publication.sid] = (participant.identity, stop_event)

        def remove_completed(completed: asyncio.Task[Any]) -> None:
            track_tasks.pop(publication.sid, None)
            track_stops.pop(publication.sid, None)
            if completed.cancelled():
                return
            error = completed.exception()
            if error is not None:
                logger.error(
                    "replay audio task failed",
                    exc_info=(type(error), error, error.__traceback__),
                    extra={
                        "process_name": "worker",
                        "session_id": target.session_id,
                        "room_name": ctx.room.name,
                        "participant_identity": participant.identity,
                        "event": "replay_audio_failed",
                        "error_type": error_category(
                            error,
                            default="asr_stream_error",
                        ),
                        "internal_error_type": type(error).__name__,
                    },
                )
                if target.is_legacy_replay:
                    ctx.shutdown(
                        "replay audio failed: "
                        f"{error_category(error, default='asr_stream_error')}"
                    )
                return
            if target.is_legacy_replay:
                ctx.shutdown("replay audio completed")
            else:
                logger.info(
                    "managed caption input completed",
                    extra={
                        "process_name": "worker",
                        "session_id": target.session_id,
                        "room_name": ctx.room.name,
                        "participant_identity": participant.identity,
                        "event": "managed_caption_input_completed",
                    },
                )

        task.add_done_callback(remove_completed)

    async def on_shutdown(reason: str) -> None:
        ctx.room.off("participant_connected", on_participant_connected)
        ctx.room.off("participant_disconnected", on_participant_disconnected)
        ctx.room.off("track_published", on_track_published)
        ctx.room.off("track_unpublished", on_track_unpublished)
        ctx.room.off("track_subscribed", on_track_subscribed)
        for _, stop_event in tuple(track_stops.values()):
            stop_event.set()
        tasks = tuple(track_tasks.values())
        await drain_track_tasks(
            tasks,
            timeout_seconds=settings.asr_finish_timeout_seconds + 5.0,
        )
        error_type = None
        if reason.startswith("replay audio failed: "):
            error_type = reason.removeprefix("replay audio failed: ")
        logger.info(
            "worker room session stopped",
            extra={
                "process_name": "worker",
                "room_name": ctx.room.name,
                "participant_identity": ctx.room.local_participant.identity,
                "event": "worker_room_disconnected",
                "error_type": error_type,
                "shutdown_reason": reason or None,
            },
        )
        worker_health_store.mark_job_finished(job_health_id)

    ctx.add_shutdown_callback(on_shutdown)
    ctx.room.on("participant_connected", on_participant_connected)
    ctx.room.on("participant_disconnected", on_participant_disconnected)
    ctx.room.on("track_published", on_track_published)
    ctx.room.on("track_unpublished", on_track_unpublished)
    ctx.room.on("track_subscribed", on_track_subscribed)
    try:
        await asyncio.wait_for(
            ctx.connect(auto_subscribe=agents.AutoSubscribe.SUBSCRIBE_NONE),
            timeout=settings.livekit_connect_timeout_seconds,
        )
    except asyncio.CancelledError:
        worker_health_store.mark_job_finished(job_health_id)
        raise
    except TimeoutError as error:
        worker_health_store.mark_job_finished(job_health_id)
        logger.error(
            "worker LiveKit connection timed out",
            extra={
                "process_name": "worker",
                "room_name": ctx.room.name,
                "event": "worker_room_connect_failed",
                "error_type": "livekit_error",
                "internal_error_type": type(error).__name__,
            },
        )
        raise LiveKitOperationError(
            "Worker LiveKit connection timed out"
        ) from error
    except BaseException as error:
        worker_health_store.mark_job_finished(job_health_id)
        logger.error(
            "worker LiveKit connection failed",
            extra={
                "process_name": "worker",
                "room_name": ctx.room.name,
                "event": "worker_room_connect_failed",
                "error_type": "livekit_error",
                "internal_error_type": type(error).__name__,
            },
        )
        raise LiveKitOperationError(
            "Worker LiveKit connection failed"
        ) from error

    for participant in ctx.room.remote_participants.values():
        on_participant_connected(participant)
        for publication in participant.track_publications.values():
            on_track_published(publication, participant)

    logger.info(
        "worker joined room",
        extra={
            "process_name": "worker",
            "room_name": ctx.room.name,
            "participant_identity": ctx.room.local_participant.identity,
            "event": "worker_room_connected",
        },
    )


worker_options = agents.WorkerOptions(
    entrypoint_fnc=worker_entrypoint,
    agent_name=settings.livekit_agent_name,
)
worker_server = agents.AgentServer.from_server_options(worker_options)


def _mark_worker_started() -> None:
    worker_health_store.mark_worker_started(os.getpid())


def _mark_worker_registered(_worker_id: str, _server_info: Any) -> None:
    worker_health_store.mark_livekit_connected()


worker_server.on("worker_started", _mark_worker_started)
worker_server.on("worker_registered", _mark_worker_registered)


if __name__ == "__main__":
    try:
        agents.cli.run_app(worker_server)
    finally:
        worker_health_store.mark_worker_stopped()
