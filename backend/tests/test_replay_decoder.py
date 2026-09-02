from __future__ import annotations

import asyncio
import subprocess
import wave
from pathlib import Path

import pytest

from app.replay.decoder import FFmpegDecodeError, FFmpegPCMDecoder


FIXTURE = Path(__file__).parent / "fixtures" / "demo_audio.wav"


def _write_silence(path: Path, samples: int, sample_rate: int = 16_000) -> None:
    with wave.open(str(path), "wb") as audio_file:
        audio_file.setnchannels(1)
        audio_file.setsampwidth(2)
        audio_file.setframerate(sample_rate)
        audio_file.writeframes(b"\x00\x00" * samples)


async def _collect(decoder: FFmpegPCMDecoder):
    return [frame async for frame in decoder.frames()]


def test_decodes_wav_to_20ms_pcm16_frames() -> None:
    decoder = FFmpegPCMDecoder(FIXTURE)

    frames = asyncio.run(_collect(decoder))

    assert len(frames) == 50
    assert all(frame.sample_rate == 16_000 for frame in frames)
    assert all(frame.channels == 1 for frame in frames)
    assert all(frame.samples_per_channel == 320 for frame in frames)
    assert all(len(frame.data) == 640 for frame in frames)
    assert sum(frame.duration_seconds for frame in frames) == pytest.approx(1.0)
    assert decoder.active_process is None


def test_decodes_mp3_input(tmp_path: Path) -> None:
    mp3_path = tmp_path / "demo.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(FIXTURE),
            str(mp3_path),
        ],
        check=True,
    )

    frames = asyncio.run(_collect(FFmpegPCMDecoder(mp3_path)))

    assert frames
    assert all(frame.sample_rate == 16_000 for frame in frames)
    assert all(frame.channels == 1 for frame in frames)


def test_preserves_a_partial_final_frame(tmp_path: Path) -> None:
    short_wav = tmp_path / "partial.wav"
    _write_silence(short_wav, samples=400)

    frames = asyncio.run(_collect(FFmpegPCMDecoder(short_wav)))

    assert [frame.samples_per_channel for frame in frames] == [320, 80]
    assert sum(frame.duration_seconds for frame in frames) == pytest.approx(0.025)


def test_rejects_unsupported_input_suffix(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="WAV or MP3"):
        FFmpegPCMDecoder(tmp_path / "audio.flac")


def test_reports_ffmpeg_decode_errors_and_reaps_process(tmp_path: Path) -> None:
    invalid_wav = tmp_path / "invalid.wav"
    invalid_wav.write_bytes(b"not a wave file")
    decoder = FFmpegPCMDecoder(invalid_wav)

    with pytest.raises(FFmpegDecodeError, match="FFmpeg failed"):
        asyncio.run(_collect(decoder))

    assert decoder.active_process is None


def test_closing_generator_reaps_ffmpeg_process(tmp_path: Path) -> None:
    long_wav = tmp_path / "long.wav"
    _write_silence(long_wav, samples=16_000 * 30)
    decoder = FFmpegPCMDecoder(long_wav)

    async def consume_one_frame() -> None:
        frames = decoder.frames()
        await anext(frames)
        await frames.aclose()

    asyncio.run(consume_one_frame())

    assert decoder.active_process is None


def test_ffmpeg_process_start_has_a_finite_timeout() -> None:
    async def blocked_process_factory(*args, **kwargs):
        await asyncio.Future()

    decoder = FFmpegPCMDecoder(
        FIXTURE,
        start_timeout_seconds=0.01,
        process_factory=blocked_process_factory,
    )

    with pytest.raises(FFmpegDecodeError, match="start timed out"):
        asyncio.run(_collect(decoder))

    assert decoder.active_process is None
