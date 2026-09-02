from __future__ import annotations

import asyncio

from app.worker.audio_router import AudioRouter


def test_optional_translation_failure_does_not_stop_source_audio() -> None:
    class SourceSink:
        def __init__(self) -> None:
            self.frames: list[bytes] = []

        def send_frame(self, pcm: bytes) -> None:
            self.frames.append(pcm)

    class FailingTranslationSink:
        def send_frame(self, _pcm: bytes) -> None:
            raise RuntimeError("translation unavailable")

    async def scenario() -> None:
        source = SourceSink()
        failures: list[str] = []
        router = AudioRouter(source)

        async def record_failure(error: BaseException) -> None:
            failures.append(str(error))

        router.add_optional(
            "translation",
            FailingTranslationSink(),
            on_failure=record_failure,
        )
        await router.route(b"frame-1")
        await router.route(b"frame-2")

        assert source.frames == [b"frame-1", b"frame-2"]
        assert failures == ["translation unavailable"]
        assert router.is_optional_active("translation") is False

    asyncio.run(scenario())
