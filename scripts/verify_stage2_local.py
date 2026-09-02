from __future__ import annotations

import argparse
import asyncio
import json
import sys
import wave
from collections.abc import AsyncIterator
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.transcription.models import ASREvent, ASREventType  # noqa: E402
from app.transcription.provider import SpeechRecognitionProvider  # noqa: E402
from app.transcription.session import TranscriptionSession  # noqa: E402


_EVENTS_DONE = object()


class LocalVerificationProvider(SpeechRecognitionProvider):
    """Deterministic provider used only to validate local Stage 2 plumbing."""

    def __init__(self) -> None:
        self.audio_chunks: list[bytes] = []
        self.partial_event_count = 0
        self.final_event_count = 0
        self.closed = False
        self._events: asyncio.Queue[ASREvent | object] = asyncio.Queue()
        self._partial_emitted = False

    async def start(self) -> None:
        await self._events.put(
            ASREvent(
                event_type=ASREventType.STREAM_STARTED,
                provider_event_id="local-started",
            )
        )

    async def send_audio(self, pcm: bytes) -> None:
        self.audio_chunks.append(pcm)
        if self._partial_emitted:
            return
        self._partial_emitted = True
        self.partial_event_count += 1
        await self._events.put(
            ASREvent(
                event_type=ASREventType.PARTIAL_RESULT,
                provider_event_id="local-partial",
                segment_id="1",
                text="本地协议验证",
            )
        )

    async def finish(self) -> None:
        self.final_event_count += 1
        await self._events.put(
            ASREvent(
                event_type=ASREventType.FINAL_RESULT,
                provider_event_id="local-final",
                segment_id="1",
                text="本地协议验证完成",
            )
        )
        await self._events.put(
            ASREvent(
                event_type=ASREventType.STREAM_COMPLETED,
                provider_event_id="local-completed",
            )
        )
        await self._events.put(_EVENTS_DONE)

    async def events(self) -> AsyncIterator[ASREvent]:
        while True:
            event = await self._events.get()
            if event is _EVENTS_DONE:
                return
            assert isinstance(event, ASREvent)
            yield event

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self._events.put(_EVENTS_DONE)


def read_pcm_frames(input_path: Path) -> list[bytes]:
    with wave.open(str(input_path), "rb") as audio:
        if audio.getnchannels() != 1:
            raise ValueError("verification WAV must be mono")
        if audio.getsampwidth() != 2:
            raise ValueError("verification WAV must be PCM16")
        if audio.getframerate() != 16_000:
            raise ValueError("verification WAV must use a 16000 Hz sample rate")

        frames: list[bytes] = []
        while frame := audio.readframes(320):
            if len(frame) % 2:
                raise ValueError("verification WAV contains an incomplete PCM16 sample")
            frames.append(frame)
        return frames


async def verify(input_path: Path) -> dict[str, object]:
    pcm_frames = read_pcm_frames(input_path)
    provider = LocalVerificationProvider()
    session = TranscriptionSession(provider_factory=lambda: provider)
    try:
        await session.start()
        for pcm in pcm_frames:
            session.send_frame(pcm)
        metrics = await session.finish()
    finally:
        await session.aclose()

    await asyncio.sleep(0)
    active_names = {
        task.get_name()
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    }
    clean_shutdown = provider.closed and not any(
        name.startswith("transcription-") for name in active_names
    )
    source_audio_bytes = sum(len(frame) for frame in pcm_frames)
    status = "ok" if clean_shutdown and metrics.provider_error_count == 0 else "failed"
    return {
        "status": status,
        "frame_count": len(pcm_frames),
        "source_audio_bytes": source_audio_bytes,
        "sent_audio_chunk_count": metrics.sent_audio_chunk_count,
        "sent_audio_bytes": metrics.sent_audio_bytes,
        "partial_event_count": provider.partial_event_count,
        "final_event_count": provider.final_event_count,
        "provider_error_count": metrics.provider_error_count,
        "clean_shutdown": clean_shutdown,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run credential-free Stage 2 ASR plumbing verification."
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=BACKEND_ROOT / "tests" / "fixtures" / "demo_audio.wav",
        dest="input_path",
    )
    args = parser.parse_args()
    input_path = args.input_path.resolve()
    if not input_path.is_file():
        parser.error(f"audio file does not exist: {input_path}")

    result = asyncio.run(verify(input_path))
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
