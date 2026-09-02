from __future__ import annotations

import pytest

from app.transcription.chunker import AudioChunker


def test_five_livekit_frames_become_one_hundred_millisecond_chunk() -> None:
    chunker = AudioChunker()
    frame = b"\x01\x02" * 320

    for _ in range(4):
        assert chunker.feed(frame) == []

    assert chunker.feed(frame) == [frame * 5]
    assert chunker.buffered_bytes == 0
    assert chunker.bytes_per_chunk == 3_200


def test_feed_can_emit_multiple_chunks_and_keeps_only_tail() -> None:
    chunker = AudioChunker()
    pcm = b"\x03\x04" * 3_360

    chunks = chunker.feed(pcm)

    assert chunks == [pcm[:3_200], pcm[3_200:6_400]]
    assert chunker.buffered_bytes == 320


def test_flush_returns_aligned_tail_once() -> None:
    chunker = AudioChunker()
    tail = b"\x05\x06" * 320
    chunker.feed(tail)

    assert chunker.flush() == tail
    assert chunker.flush() is None
    assert chunker.buffered_bytes == 0


def test_odd_length_pcm16_is_rejected_without_mutating_buffer() -> None:
    chunker = AudioChunker()

    with pytest.raises(ValueError, match="PCM16"):
        chunker.feed(b"\x00")

    assert chunker.buffered_bytes == 0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"sample_rate": 0}, "sample_rate"),
        ({"channels": 0}, "channels"),
        ({"sample_width_bytes": 1}, "PCM16"),
        ({"chunk_duration_ms": 0}, "chunk_duration_ms"),
        (
            {"sample_rate": 11_025, "chunk_duration_ms": 1},
            "whole number of bytes",
        ),
    ],
)
def test_invalid_audio_settings_are_rejected(
    kwargs: dict[str, int],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        AudioChunker(**kwargs)
