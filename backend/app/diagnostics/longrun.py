from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psutil
from sqlalchemy import select

from app.captions.events import LIVE_CAPTION_TOPIC
from app.captions.runtime import create_worker_caption_runtime
from app.hls.decoder import FFmpegHLSDecoder
from app.persistence.database import Database
from app.persistence.models import (
    SegmentRecord,
    SessionRecord,
    TranslationSegmentRecord,
)
from app.replay.clock import ReplayClock
from app.replay.decoder import FFmpegPCMDecoder, PCMFrame
from app.settings import Settings
from app.transcription.bailian import (
    BailianConfig,
    BailianSpeechRecognitionProvider,
)
from app.transcription.fake import FakeASRConfig, FakeASRProvider
from app.transcription.session import TranscriptionSession
from app.translation.bailian import (
    BailianLiveTranslateConfig,
    BailianLiveTranslateProvider,
)
from app.translation.fake import (
    FakeTranslationConfig,
    FakeTranslationProvider,
)
from app.translation.runtime import create_worker_translation_runtime
from app.translation.session import TranslationSession
from app.worker.entrypoint import consume_replay_audio
from app.worker.transport_runtime import (
    TransportFrameSink,
    TransportMetrics,
    active_task_count,
    process_memory_mb,
)


class LongRunConfigurationError(ValueError):
    """Raised before a diagnostic could make an unsafe or ambiguous run."""


class LongRunMode(StrEnum):
    TRANSPORT_ONLY = "transport-only"
    FAKE_PROVIDER = "fake-provider"
    REAL_PROVIDER_SMOKE = "real-provider-smoke"


class LongRunSourceType(StrEnum):
    LOCAL_MEDIA = "local-media"
    M3U8 = "m3u8"


class FailureInjection(StrEnum):
    DUPLICATE_FINAL = "duplicate-final"
    STALE_PARTIAL = "stale-partial"
    TRANSLATION_FAILURE = "translation-failure"
    ASR_FAILURE = "asr-failure"
    SLOW_CONSUMER = "slow-consumer"
    PROVIDER_TIMEOUT = "provider-timeout"


_FAKE_ONLY_INJECTIONS = {
    FailureInjection.DUPLICATE_FINAL,
    FailureInjection.STALE_PARTIAL,
    FailureInjection.TRANSLATION_FAILURE,
    FailureInjection.ASR_FAILURE,
    FailureInjection.PROVIDER_TIMEOUT,
}


@dataclass(frozen=True, slots=True)
class LongRunConfig:
    mode: LongRunMode
    source_type: LongRunSourceType
    source: str
    duration_minutes: float
    reports_dir: Path
    allow_cloud: bool = False
    with_translation: bool = False
    source_language: str = "zh-CN"
    target_language: str = "en-US"
    failure_injections: frozenset[FailureInjection] = frozenset()
    queue_max_frames: int = 100
    consumer_delay_ms: float = 0.0
    fake_chunks_per_segment: int = 3

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", LongRunMode(self.mode))
        object.__setattr__(
            self,
            "source_type",
            LongRunSourceType(self.source_type),
        )
        object.__setattr__(self, "reports_dir", Path(self.reports_dir))
        object.__setattr__(
            self,
            "failure_injections",
            frozenset(
                FailureInjection(item) for item in self.failure_injections
            ),
        )
        self.validate()

    def validate(self) -> None:
        if not math.isfinite(self.duration_minutes):
            raise LongRunConfigurationError("duration must be finite")
        if self.duration_minutes <= 0:
            raise LongRunConfigurationError("duration must be positive")
        if not self.source or not self.source.strip():
            raise LongRunConfigurationError("source is required")
        if self.queue_max_frames <= 0:
            raise LongRunConfigurationError(
                "queue_max_frames must be positive"
            )
        if self.consumer_delay_ms < 0:
            raise LongRunConfigurationError(
                "consumer_delay_ms must be non-negative"
            )
        if not 3 <= self.fake_chunks_per_segment <= 600:
            raise LongRunConfigurationError(
                "fake_chunks_per_segment must be between 3 and 600"
            )
        if self.source_type is LongRunSourceType.M3U8:
            scheme = urlsplit(self.source).scheme.lower()
            if scheme not in {"http", "https"}:
                raise LongRunConfigurationError(
                    "M3U8 source must use http:// or https://"
                )
        if self.mode is LongRunMode.REAL_PROVIDER_SMOKE:
            if not self.allow_cloud:
                raise LongRunConfigurationError(
                    "real-provider-smoke requires explicit --allow-cloud"
                )
            if not 3 <= self.duration_minutes <= 10:
                raise LongRunConfigurationError(
                    "real-provider-smoke duration must be between 3 and 10 minutes"
                )
            if self.failure_injections:
                raise LongRunConfigurationError(
                    "failure injection is not available for real providers"
                )
        elif self.duration_minutes > 180:
            raise LongRunConfigurationError(
                "local diagnostic duration cannot exceed 180 minutes"
            )
        if (
            self.mode is not LongRunMode.FAKE_PROVIDER
            and self.failure_injections & _FAKE_ONLY_INJECTIONS
        ):
            raise LongRunConfigurationError(
                "provider failure injection requires fake-provider mode"
            )
        if (
            FailureInjection.TRANSLATION_FAILURE
            in self.failure_injections
            and not self.with_translation
        ):
            raise LongRunConfigurationError(
                "translation-failure requires --with-translation"
            )


@dataclass(slots=True)
class LongRunReport:
    test_mode: str
    source_type: str
    duration_minutes: float
    start_memory_mb: float
    end_memory_mb: float
    peak_memory_mb: float
    frame_count: int
    audio_bytes: int
    max_queue_size: int
    dropped_frames: int
    first_partial_latency_ms: float | None
    final_count: int
    translation_final_count: int
    ffmpeg_process_count_end: int
    active_task_count_end: int
    room_connected_end: bool
    provider_connected_end: bool
    exit_reason: str
    errors: list[str] = field(default_factory=list)
    requested_duration_minutes: float | None = None
    audio_duration_ms: float = 0.0
    receive_wall_time_ms: float = 0.0
    realtime_ratio: float | None = None
    source: str = ""
    failure_injections: list[str] = field(default_factory=list)
    database_path: str | None = None
    livekit_event_count: int = 0
    source_abort_count: int = 0
    fake_chunks_per_segment: int | None = None
    started_at: str = ""
    ended_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LongRunArtifacts:
    report: LongRunReport
    json_path: Path
    markdown_path: Path


@dataclass(frozen=True, slots=True)
class _AudioFrame:
    data: bytes
    sample_rate: int
    num_channels: int
    samples_per_channel: int

    @property
    def duration_seconds(self) -> float:
        return self.samples_per_channel / self.sample_rate


@dataclass(frozen=True, slots=True)
class _AudioEvent:
    frame: _AudioFrame


class _DiagnosticStream:
    def __init__(
        self,
        config: LongRunConfig,
        *,
        memory_observer: Callable[[float], None],
    ) -> None:
        self._iterator = _iter_source_frames(config)
        self._memory_observer = memory_observer
        self.frame_count = 0
        self.audio_bytes = 0
        self.audio_duration_ms = 0.0
        self.closed = False

    def __aiter__(self) -> _DiagnosticStream:
        return self

    async def __anext__(self) -> _AudioEvent:
        frame = await anext(self._iterator)
        self.frame_count += 1
        self.audio_bytes += len(frame.data)
        self.audio_duration_ms += frame.duration_seconds * 1_000
        self._memory_observer(process_memory_mb())
        return _AudioEvent(frame)

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self._iterator.aclose()


class _RecordingParticipant:
    def __init__(self) -> None:
        self.packets: list[dict[str, Any]] = []

    async def publish_data(
        self,
        data: bytes,
        *,
        reliable: bool,
        topic: str,
    ) -> None:
        if not reliable or topic != LIVE_CAPTION_TOPIC:
            raise RuntimeError("diagnostic publisher contract was violated")
        self.packets.append(json.loads(data))


def _normalize_frame(frame: PCMFrame) -> _AudioFrame:
    return _AudioFrame(
        data=frame.data,
        sample_rate=frame.sample_rate,
        num_channels=frame.channels,
        samples_per_channel=frame.samples_per_channel,
    )


def _redact_source(source: str, source_type: LongRunSourceType) -> str:
    if source_type is LongRunSourceType.LOCAL_MEDIA:
        return str(Path(source).resolve())
    parts = urlsplit(source)
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def count_ffmpeg_processes() -> int:
    count = 0
    for process in psutil.process_iter(("name",)):
        try:
            name = (process.info["name"] or "").lower()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        if name in {"ffmpeg", "ffmpeg.exe"}:
            count += 1
    return count


async def _iter_source_frames(
    config: LongRunConfig,
) -> AsyncIterator[_AudioFrame]:
    target_seconds = config.duration_minutes * 60
    elapsed_seconds = 0.0
    replay_clock = ReplayClock()

    if config.source_type is LongRunSourceType.M3U8:
        decoder = FFmpegHLSDecoder(config.source)
        iterator = decoder.frames()
        try:
            async for frame in iterator:
                normalized = _normalize_frame(frame)
                await replay_clock.wait_for_frame(
                    normalized.duration_seconds
                )
                yield normalized
                elapsed_seconds += normalized.duration_seconds
                if elapsed_seconds >= target_seconds:
                    break
        finally:
            await iterator.aclose()
            await decoder.aclose()
        if elapsed_seconds < target_seconds:
            raise RuntimeError(
                "M3U8 source ended before the requested duration"
            )
        await replay_clock.wait_until_complete()
        return

    media_path = Path(config.source).resolve()
    if not media_path.is_file():
        raise LongRunConfigurationError(
            f"local media source does not exist: {media_path}"
        )

    while elapsed_seconds < target_seconds:
        decoder = FFmpegPCMDecoder(media_path)
        iterator = decoder.frames()
        emitted_this_loop = False
        try:
            async for frame in iterator:
                emitted_this_loop = True
                normalized = _normalize_frame(frame)
                await replay_clock.wait_for_frame(
                    normalized.duration_seconds
                )
                yield normalized
                elapsed_seconds += normalized.duration_seconds
                if elapsed_seconds >= target_seconds:
                    break
        finally:
            await iterator.aclose()
        if not emitted_this_loop:
            raise RuntimeError("local media source produced no audio frames")
    await replay_clock.wait_until_complete()


async def _run_transport_only(
    config: LongRunConfig,
) -> tuple[TransportMetrics, str, list[str]]:
    consumer_delay = config.consumer_delay_ms / 1_000
    if FailureInjection.SLOW_CONSUMER in config.failure_injections:
        consumer_delay = max(consumer_delay, 0.04)
    sink = TransportFrameSink(
        queue_max_frames=config.queue_max_frames,
        consumer_delay_seconds=consumer_delay,
    )
    errors: list[str] = []
    exit_reason = "completed"
    try:
        await sink.start()
        async for frame in _iter_source_frames(config):
            await sink.send_frame(frame)
        metrics = await sink.finish()
        return metrics, exit_reason, errors
    except BaseException as error:
        errors.append(f"{type(error).__name__}: {error}")
        exit_reason = (
            "injected_backpressure"
            if FailureInjection.SLOW_CONSUMER
            in config.failure_injections
            else "failed"
        )
        return sink.metrics, exit_reason, errors
    finally:
        await sink.aclose()


def _fake_asr_config(config: LongRunConfig) -> FakeASRConfig:
    injections = config.failure_injections
    return FakeASRConfig(
        chunks_per_segment=config.fake_chunks_per_segment,
        emit_duplicate_final=(
            FailureInjection.DUPLICATE_FINAL in injections
        ),
        emit_stale_partial=(
            FailureInjection.STALE_PARTIAL in injections
        ),
        fail_after_chunks=(
            5 if FailureInjection.ASR_FAILURE in injections else None
        ),
        send_delay_seconds=(
            max(config.consumer_delay_ms / 1_000, 0.15)
            if FailureInjection.SLOW_CONSUMER in injections
            else config.consumer_delay_ms / 1_000
        ),
        timeout_on_finish=(
            FailureInjection.PROVIDER_TIMEOUT in injections
        ),
    )


def _fake_translation_config(
    config: LongRunConfig,
) -> FakeTranslationConfig:
    injections = config.failure_injections
    return FakeTranslationConfig(
        target_language=config.target_language,
        chunks_per_segment=config.fake_chunks_per_segment,
        emit_duplicate_final=(
            FailureInjection.DUPLICATE_FINAL in injections
        ),
        emit_stale_partial=(
            FailureInjection.STALE_PARTIAL in injections
        ),
        fail_after_chunks=(
            5
            if FailureInjection.TRANSLATION_FAILURE in injections
            else None
        ),
        send_delay_seconds=(
            max(config.consumer_delay_ms / 1_000, 0.15)
            if FailureInjection.SLOW_CONSUMER in injections
            else config.consumer_delay_ms / 1_000
        ),
        timeout_on_finish=(
            FailureInjection.PROVIDER_TIMEOUT in injections
        ),
    )


def _real_transcription_factory(
    settings: Settings,
    config: LongRunConfig,
) -> Callable[[], BailianSpeechRecognitionProvider]:
    return lambda: BailianSpeechRecognitionProvider(
        BailianConfig(
            api_key=settings.dashscope_api_key,
            workspace_id=settings.dashscope_workspace_id,
            region=settings.dashscope_region,
            model=settings.bailian_asr_model,
            websocket_url=settings.dashscope_websocket_url,
            start_timeout_seconds=settings.asr_start_timeout_seconds,
            finish_timeout_seconds=settings.asr_finish_timeout_seconds,
            language=config.source_language,
        )
    )


def _real_translation_factory(
    settings: Settings,
    config: LongRunConfig,
) -> Callable[[], BailianLiveTranslateProvider]:
    return lambda: BailianLiveTranslateProvider(
        BailianLiveTranslateConfig(
            api_key=settings.dashscope_api_key,
            workspace_id=settings.dashscope_workspace_id,
            region=settings.dashscope_region,
            model=settings.bailian_translation_model,
            websocket_url=settings.dashscope_translation_websocket_url,
            source_language=config.source_language,
            target_language=config.target_language,
            start_timeout_seconds=(
                settings.translation_start_timeout_seconds
            ),
            finish_timeout_seconds=(
                settings.translation_finish_timeout_seconds
            ),
        )
    )


async def _run_provider_mode(
    config: LongRunConfig,
    *,
    settings: Settings,
) -> dict[str, Any]:
    start_memory = process_memory_mb()
    peak_memory = start_memory
    started_at = time.monotonic()
    errors: list[str] = []
    exit_reason = "completed"
    peak_holder = [peak_memory]
    transcription_sessions: list[TranscriptionSession] = []
    translation_sessions: list[TranslationSession] = []
    source_abort_requests: list[tuple[str, str]] = []

    config.reports_dir.mkdir(parents=True, exist_ok=True)
    diagnostic_database_path = (
        config.reports_dir.resolve()
        / f"stage1_5_longrun_runtime_{uuid.uuid4().hex}.db"
    )
    database_url = f"sqlite:///{diagnostic_database_path.as_posix()}"
    database = Database(database_url)
    database.create_schema()
    session_id = str(uuid.uuid4())
    room_name = "stage1-5-diagnostic"
    with database.session() as db_session:
        db_session.add(
            SessionRecord(
                id=session_id,
                room_name=room_name,
                status="created",
                source_type=(
                    "hls"
                    if config.source_type is LongRunSourceType.M3U8
                    else "file"
                ),
                source_name=_redact_source(
                    config.source,
                    config.source_type,
                )[-255:],
                language=config.source_language,
                target_language=(
                    config.target_language
                    if config.with_translation
                    else None
                ),
            )
        )
        db_session.commit()

    def observe_memory(value: float) -> None:
        peak_holder[0] = max(peak_holder[0], value)

    stream = _DiagnosticStream(
        config,
        memory_observer=observe_memory,
    )
    participant = _RecordingParticipant()

    if config.mode is LongRunMode.FAKE_PROVIDER:
        provider_name = "fake"
        asr_model = "fake-asr-v1"
        translation_model = "fake-translation-v1"
        transcription_provider_factory: Callable[[], Any] = lambda: FakeASRProvider(
            _fake_asr_config(config)
        )
        translation_provider_factory: Callable[[], Any] = (
            lambda: FakeTranslationProvider(
                _fake_translation_config(config)
            )
        )
    else:
        provider_name = "bailian"
        asr_model = settings.bailian_asr_model
        translation_model = settings.bailian_translation_model
        transcription_provider_factory = _real_transcription_factory(
            settings,
            config,
        )
        translation_provider_factory = _real_translation_factory(
            settings,
            config,
        )

    def transcription_session_factory(**kwargs: Any) -> TranscriptionSession:
        session = TranscriptionSession(
            provider_factory=transcription_provider_factory,
            queue_max_chunks=settings.asr_queue_max_chunks,
            chunk_duration_ms=settings.asr_chunk_duration_ms,
            startup_retries=(
                0
                if config.mode is LongRunMode.FAKE_PROVIDER
                else settings.asr_startup_retries
            ),
            event_handler=kwargs["event_handler"],
            log_context={
                "session_id": session_id,
                "room_name": room_name,
                "participant_identity": f"replay-{session_id}",
            },
        )
        transcription_sessions.append(session)
        return session

    def translation_session_factory(**kwargs: Any) -> TranslationSession:
        session = TranslationSession(
            provider_factory=translation_provider_factory,
            queue_max_chunks=settings.translation_queue_max_chunks,
            chunk_duration_ms=settings.asr_chunk_duration_ms,
            event_handler=kwargs["event_handler"],
        )
        translation_sessions.append(session)
        return session

    caption_runtime = create_worker_caption_runtime(
        session_id=session_id,
        track_id="stage1-5-diagnostic-track",
        local_participant=participant,
        database_url=database_url,
        provider_name=provider_name,
        model_name=asr_model,
    )
    translation_runtime = create_worker_translation_runtime(
        session_id=session_id,
        database_url=database_url,
        provider_name=provider_name,
        model_name=translation_model,
        local_participant=participant,
    )

    async def record_source_abort(reason: str, detail: str) -> None:
        source_abort_requests.append((reason, detail))

    try:
        await consume_replay_audio(
            track="stage1-5-diagnostic-track",
            room_name=room_name,
            participant_identity=f"replay-{session_id}",
            session_id=session_id,
            stream_factory=lambda *args, **kwargs: stream,
            transcription_session_factory=(
                transcription_session_factory
            ),
            caption_runtime=caption_runtime,
            translation_session_factory=translation_session_factory,
            translation_runtime=translation_runtime,
            source_abort_handler=record_source_abort,
        )
    except BaseException as error:
        errors.append(f"{type(error).__name__}: {error}")
        if FailureInjection.ASR_FAILURE in config.failure_injections:
            exit_reason = "injected_asr_failure"
        elif FailureInjection.PROVIDER_TIMEOUT in config.failure_injections:
            exit_reason = "injected_provider_timeout"
        elif "Backpressure" in type(error).__name__:
            exit_reason = "injected_backpressure"
        else:
            exit_reason = "failed"
    finally:
        await stream.aclose()

    wall_time_ms = (time.monotonic() - started_at) * 1_000
    end_memory = process_memory_mb()
    peak_holder[0] = max(peak_holder[0], end_memory)
    try:
        with database.session() as db_session:
            source_finals = list(
                db_session.scalars(
                    select(SegmentRecord).where(
                        SegmentRecord.session_id == session_id
                    )
                )
            )
            translation_finals = list(
                db_session.scalars(
                    select(TranslationSegmentRecord).where(
                        TranslationSegmentRecord.session_id == session_id
                    )
                )
            )
            record = db_session.get(SessionRecord, session_id)
            if (
                exit_reason == "completed"
                and record is not None
                and record.translation_status == "failed"
            ):
                exit_reason = "translation_degraded"
                if record.translation_error_code:
                    errors.append(
                        "translation " + record.translation_error_code
                    )
    finally:
        database.dispose()

    first_partial_latency_ms = (
        transcription_sessions[0].metrics.first_partial_latency_ms
        if transcription_sessions
        else None
    )
    max_queue_size = max(
        [session.max_queue_size for session in transcription_sessions]
        + [session.max_queue_size for session in translation_sessions]
        + [0]
    )
    dropped_frames = int(
        any("Backpressure" in error for error in errors)
    )
    return {
        "start_memory_mb": start_memory,
        "end_memory_mb": end_memory,
        "peak_memory_mb": peak_holder[0],
        "frame_count": stream.frame_count,
        "audio_bytes": stream.audio_bytes,
        "audio_duration_ms": stream.audio_duration_ms,
        "receive_wall_time_ms": wall_time_ms,
        "realtime_ratio": (
            stream.audio_duration_ms / wall_time_ms
            if wall_time_ms > 0
            else None
        ),
        "max_queue_size": max_queue_size,
        "dropped_frames": dropped_frames,
        "first_partial_latency_ms": first_partial_latency_ms,
        "final_count": len(source_finals),
        "translation_final_count": len(translation_finals),
        "exit_reason": exit_reason,
        "errors": errors,
        "database_path": str(diagnostic_database_path),
        "livekit_event_count": len(participant.packets),
        "source_abort_count": len(source_abort_requests),
    }


async def execute_longrun(
    config: LongRunConfig,
    *,
    settings: Settings | None = None,
) -> LongRunReport:
    config.validate()
    resolved_settings = settings or Settings()
    started_at = dt.datetime.now(dt.UTC)

    if config.mode is LongRunMode.TRANSPORT_ONLY:
        metrics, exit_reason, errors = await _run_transport_only(config)
        values: dict[str, Any] = {
            "start_memory_mb": metrics.start_memory_mb,
            "end_memory_mb": metrics.end_memory_mb,
            "peak_memory_mb": metrics.peak_memory_mb,
            "frame_count": metrics.frame_count,
            "audio_bytes": metrics.audio_bytes,
            "audio_duration_ms": metrics.audio_duration_ms,
            "receive_wall_time_ms": metrics.receive_wall_time_ms,
            "realtime_ratio": metrics.realtime_ratio,
            "max_queue_size": metrics.max_queue_size,
            "dropped_frames": metrics.dropped_frames,
            "first_partial_latency_ms": None,
            "final_count": 0,
            "translation_final_count": 0,
            "exit_reason": exit_reason,
            "errors": errors,
            "database_path": None,
            "livekit_event_count": 0,
            "source_abort_count": 0,
        }
    else:
        values = await _run_provider_mode(
            config,
            settings=resolved_settings,
        )

    ended_at = dt.datetime.now(dt.UTC)
    return LongRunReport(
        test_mode=config.mode.value,
        source_type=config.source_type.value,
        duration_minutes=(
            values["receive_wall_time_ms"] / 60_000
        ),
        requested_duration_minutes=config.duration_minutes,
        start_memory_mb=round(values["start_memory_mb"], 3),
        end_memory_mb=round(values["end_memory_mb"], 3),
        peak_memory_mb=round(values["peak_memory_mb"], 3),
        frame_count=values["frame_count"],
        audio_bytes=values["audio_bytes"],
        audio_duration_ms=round(values["audio_duration_ms"], 3),
        receive_wall_time_ms=round(
            values["receive_wall_time_ms"],
            3,
        ),
        realtime_ratio=(
            round(values["realtime_ratio"], 6)
            if values["realtime_ratio"] is not None
            else None
        ),
        max_queue_size=values["max_queue_size"],
        dropped_frames=values["dropped_frames"],
        first_partial_latency_ms=(
            round(values["first_partial_latency_ms"], 3)
            if values["first_partial_latency_ms"] is not None
            else None
        ),
        final_count=values["final_count"],
        translation_final_count=values["translation_final_count"],
        ffmpeg_process_count_end=count_ffmpeg_processes(),
        active_task_count_end=active_task_count(),
        room_connected_end=False,
        provider_connected_end=False,
        exit_reason=values["exit_reason"],
        errors=values["errors"],
        source=_redact_source(config.source, config.source_type),
        failure_injections=sorted(
            item.value for item in config.failure_injections
        ),
        database_path=values["database_path"],
        livekit_event_count=values["livekit_event_count"],
        source_abort_count=values["source_abort_count"],
        fake_chunks_per_segment=(
            config.fake_chunks_per_segment
            if config.mode is LongRunMode.FAKE_PROVIDER
            else None
        ),
        started_at=started_at.isoformat(),
        ended_at=ended_at.isoformat(),
    )


def _markdown_value(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "none"
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown_report(report: LongRunReport) -> str:
    rows = ["# Stage 1.5 Long-run Report", "", "| Field | Value |", "|---|---|"]
    rows.extend(
        f"| `{key}` | {_markdown_value(value)} |"
        for key, value in report.to_dict().items()
    )
    rows.append("")
    return "\n".join(rows)


def write_longrun_report(
    report: LongRunReport,
    reports_dir: str | Path,
    *,
    timestamp: dt.datetime | None = None,
) -> LongRunArtifacts:
    output_dir = Path(reports_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    resolved_timestamp = timestamp or dt.datetime.now(dt.UTC)
    suffix = resolved_timestamp.strftime("%Y%m%d-%H%M%S-%f")
    stem = f"stage1_5_longrun_{suffix}"
    json_path = output_dir / f"{stem}.json"
    markdown_path = output_dir / f"{stem}.md"
    json_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(
        render_markdown_report(report),
        encoding="utf-8",
    )
    return LongRunArtifacts(
        report=report,
        json_path=json_path,
        markdown_path=markdown_path,
    )
