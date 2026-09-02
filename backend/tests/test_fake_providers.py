from __future__ import annotations

import asyncio

import pytest

from app.transcription.errors import ASRProviderError, ASRTimeoutError
from app.transcription.fake import FakeASRConfig, FakeASRProvider
from app.transcription.models import ASREventType
from app.translation.errors import (
    TranslationProviderError,
    TranslationTimeoutError,
)
from app.translation.fake import (
    FakeTranslationConfig,
    FakeTranslationProvider,
)
from app.translation.models import TranslationEventType


async def collect(provider) -> list:
    return [event async for event in provider.events()]


def test_fake_asr_emits_v1_v2_v3_duplicate_and_stale_events() -> None:
    async def scenario() -> None:
        provider = FakeASRProvider(
            FakeASRConfig(
                emit_duplicate_final=True,
                emit_stale_partial=True,
            )
        )
        await provider.start()
        for _ in range(3):
            await provider.send_audio(b"\0" * 3_200)
        await provider.finish()
        events = await collect(provider)
        await provider.aclose()

        assert [event.event_type for event in events] == [
            ASREventType.STREAM_STARTED,
            ASREventType.PARTIAL_RESULT,
            ASREventType.PARTIAL_RESULT,
            ASREventType.FINAL_RESULT,
            ASREventType.FINAL_RESULT,
            ASREventType.PARTIAL_RESULT,
            ASREventType.STREAM_COMPLETED,
        ]
        assert [event.text for event in events[1:6]] == [
            "fake transcript 1 v1",
            "fake transcript 1 v2",
            "fake transcript 1 v3",
            "fake transcript 1 v3",
            "fake transcript 1 stale-v1",
        ]

    asyncio.run(scenario())


def test_fake_asr_supports_failure_slow_send_and_timeout() -> None:
    async def scenario() -> None:
        failing = FakeASRProvider(
            FakeASRConfig(fail_after_chunks=1, send_delay_seconds=0.001)
        )
        await failing.start()
        with pytest.raises(ASRProviderError, match="injected"):
            await failing.send_audio(b"audio")
        await failing.aclose()

        timing_out = FakeASRProvider(FakeASRConfig(timeout_on_finish=True))
        await timing_out.start()
        with pytest.raises(ASRTimeoutError, match="timeout"):
            await timing_out.finish()
        await timing_out.aclose()

    asyncio.run(scenario())


def test_fake_translation_matches_lifecycle_and_failure_contracts() -> None:
    async def scenario() -> None:
        provider = FakeTranslationProvider(
            FakeTranslationConfig(
                target_language="en-US",
                emit_duplicate_final=True,
                emit_stale_partial=True,
            )
        )
        await provider.start()
        for _ in range(3):
            await provider.send_audio(b"\0" * 3_200)
        await provider.finish()
        events = await collect(provider)
        await provider.aclose()

        assert [event.event_type for event in events] == [
            TranslationEventType.STREAM_STARTED,
            TranslationEventType.PARTIAL_RESULT,
            TranslationEventType.PARTIAL_RESULT,
            TranslationEventType.FINAL_RESULT,
            TranslationEventType.FINAL_RESULT,
            TranslationEventType.PARTIAL_RESULT,
            TranslationEventType.STREAM_COMPLETED,
        ]
        assert all(event.target_language == "en-US" for event in events)

        failing = FakeTranslationProvider(
            FakeTranslationConfig(
                target_language="en-US",
                fail_after_chunks=1,
            )
        )
        await failing.start()
        with pytest.raises(TranslationProviderError, match="injected"):
            await failing.send_audio(b"audio")
        await failing.aclose()

        timing_out = FakeTranslationProvider(
            FakeTranslationConfig(
                target_language="en-US",
                timeout_on_finish=True,
            )
        )
        await timing_out.start()
        with pytest.raises(TranslationTimeoutError, match="timeout"):
            await timing_out.finish()
        await timing_out.aclose()

    asyncio.run(scenario())

