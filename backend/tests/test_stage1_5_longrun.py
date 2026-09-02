from __future__ import annotations

import asyncio
import json
from dataclasses import fields
from pathlib import Path

import pytest

from app.diagnostics.longrun import (
    FailureInjection,
    LongRunConfig,
    LongRunConfigurationError,
    LongRunMode,
    LongRunReport,
    LongRunSourceType,
    execute_longrun,
    write_longrun_report,
)
from app.settings import Settings


FIXTURE = Path(__file__).parent / "fixtures" / "demo_audio.wav"
REQUIRED_REPORT_FIELDS = {
    "test_mode",
    "source_type",
    "duration_minutes",
    "start_memory_mb",
    "end_memory_mb",
    "peak_memory_mb",
    "frame_count",
    "audio_bytes",
    "max_queue_size",
    "dropped_frames",
    "first_partial_latency_ms",
    "final_count",
    "translation_final_count",
    "ffmpeg_process_count_end",
    "active_task_count_end",
    "room_connected_end",
    "provider_connected_end",
    "exit_reason",
    "errors",
}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="test-key",
        livekit_api_secret="test-secret-that-is-at-least-32-bytes",
        livekit_room_name="test-room",
        data_dir=tmp_path,
    )


def _config(tmp_path: Path, **overrides) -> LongRunConfig:
    values = {
        "mode": LongRunMode.TRANSPORT_ONLY,
        "source_type": LongRunSourceType.LOCAL_MEDIA,
        "source": str(FIXTURE),
        "duration_minutes": 0.001,
        "reports_dir": tmp_path,
    }
    values.update(overrides)
    return LongRunConfig(**values)


@pytest.mark.parametrize("duration", [0, -1, float("inf"), float("nan")])
def test_longrun_requires_a_finite_positive_duration(
    tmp_path: Path,
    duration: float,
) -> None:
    with pytest.raises(LongRunConfigurationError):
        _config(tmp_path, duration_minutes=duration)


def test_real_provider_smoke_requires_acknowledgement_and_is_bounded(
    tmp_path: Path,
) -> None:
    with pytest.raises(LongRunConfigurationError, match="allow-cloud"):
        _config(
            tmp_path,
            mode=LongRunMode.REAL_PROVIDER_SMOKE,
            duration_minutes=3,
        )

    with pytest.raises(LongRunConfigurationError, match="between 3 and 10"):
        _config(
            tmp_path,
            mode=LongRunMode.REAL_PROVIDER_SMOKE,
            duration_minutes=11,
            allow_cloud=True,
        )


def test_failure_injection_is_restricted_to_compatible_fake_mode(
    tmp_path: Path,
) -> None:
    with pytest.raises(LongRunConfigurationError, match="fake-provider"):
        _config(
            tmp_path,
            failure_injections=frozenset(
                {FailureInjection.DUPLICATE_FINAL}
            ),
        )

    with pytest.raises(LongRunConfigurationError, match="translation"):
        _config(
            tmp_path,
            mode=LongRunMode.FAKE_PROVIDER,
            failure_injections=frozenset(
                {FailureInjection.TRANSLATION_FAILURE}
            ),
        )


@pytest.mark.parametrize("chunks_per_segment", [2, 601])
def test_fake_segment_cadence_is_bounded(
    tmp_path: Path,
    chunks_per_segment: int,
) -> None:
    with pytest.raises(
        LongRunConfigurationError,
        match="fake_chunks_per_segment",
    ):
        _config(
            tmp_path,
            mode=LongRunMode.FAKE_PROVIDER,
            fake_chunks_per_segment=chunks_per_segment,
        )


def test_report_writes_all_taskbook_fields_as_utf8_json_and_markdown(
    tmp_path: Path,
) -> None:
    report = LongRunReport(
        test_mode="transport-only",
        source_type="local-media",
        duration_minutes=0.1,
        start_memory_mb=100,
        end_memory_mb=101,
        peak_memory_mb=102,
        frame_count=10,
        audio_bytes=6400,
        max_queue_size=1,
        dropped_frames=0,
        first_partial_latency_ms=None,
        final_count=0,
        translation_final_count=0,
        ffmpeg_process_count_end=0,
        active_task_count_end=1,
        room_connected_end=False,
        provider_connected_end=False,
        exit_reason="completed",
        errors=[],
        source="测试音频.wav",
    )
    artifacts = write_longrun_report(report, tmp_path)

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    markdown = artifacts.markdown_path.read_text(encoding="utf-8")
    assert REQUIRED_REPORT_FIELDS <= payload.keys()
    assert REQUIRED_REPORT_FIELDS <= {
        item.name for item in fields(LongRunReport)
    }
    assert payload["source"] == "测试音频.wav"
    for field_name in REQUIRED_REPORT_FIELDS:
        assert f"`{field_name}`" in markdown


def test_short_transport_and_fake_provider_runs_are_local_and_bounded(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    transport = asyncio.run(
        execute_longrun(_config(tmp_path), settings=settings)
    )
    fake = asyncio.run(
        execute_longrun(
            _config(
                tmp_path,
                mode=LongRunMode.FAKE_PROVIDER,
                duration_minutes=0.006,
                with_translation=True,
                failure_injections=frozenset(
                    {
                        FailureInjection.DUPLICATE_FINAL,
                        FailureInjection.STALE_PARTIAL,
                    }
                ),
            ),
            settings=settings,
        )
    )

    assert transport.exit_reason == "completed"
    assert transport.frame_count >= 3
    assert transport.audio_bytes > 0
    assert transport.dropped_frames == 0
    assert transport.provider_connected_end is False
    assert fake.exit_reason == "completed"
    assert fake.final_count == 2
    assert fake.translation_final_count == 2
    assert fake.first_partial_latency_ms is not None
    assert fake.provider_connected_end is False
    assert fake.database_path is not None
    assert Path(fake.database_path).is_file()
    assert fake.livekit_event_count > 0
    assert fake.fake_chunks_per_segment == 3
