from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.translation.models import TranslationEvent, TranslationEventType
from app.translation.runtime import WorkerTranslationRuntime


def test_translation_runtime_persists_final_before_publish() -> None:
    operations: list[object] = []

    class FakeSessionRepository:
        def mark_translation_starting(self, session_id, **kwargs):
            operations.append(("starting", session_id, kwargs))

        def mark_translation_active(self, session_id):
            operations.append(("active", session_id))

        def begin_translation_finalizing(self, session_id):
            operations.append(("finalizing", session_id))

        def complete_translation(self, session_id):
            operations.append(("completed", session_id))

        def fail_translation(self, session_id, **kwargs):
            operations.append(("failed", session_id, kwargs))

    class FakeTranslationRepository:
        def upsert_final(self, caption):
            operations.append(("persist", caption.text, caption.revision))
            return SimpleNamespace(source_segment_ids=["source-1"])

    class FakeDatabaseSession:
        def commit(self):
            operations.append("commit")

        def rollback(self):
            operations.append("rollback")

        def close(self):
            operations.append("close")

    class FakePublisher:
        async def publish_translation_status(self, **payload):
            operations.append(("status", payload["status"]))

        async def publish_translation(self, caption):
            operations.append(
                ("publish", caption.text, caption.source_segment_ids)
            )

    async def scenario() -> None:
        clock_values = iter((0.0, 100.0, 260.0))
        runtime = WorkerTranslationRuntime(
            session_id="session-1",
            source_language="zh-CN",
            target_language="en-US",
            provider_name="bailian",
            model_name="qwen3.5-livetranslate-flash-realtime",
            session_repository=FakeSessionRepository(),  # type: ignore[arg-type]
            translation_repository=FakeTranslationRepository(),  # type: ignore[arg-type]
            db_session=FakeDatabaseSession(),  # type: ignore[arg-type]
            publisher=FakePublisher(),  # type: ignore[arg-type]
            draft_publish_interval_ms=250.0,
            clock_ms=lambda: next(clock_values),
        )
        await runtime.start()
        await runtime.handle_event(
            TranslationEvent(
                event_type=TranslationEventType.STREAM_STARTED,
                provider_event_id="started",
                target_language="en",
            )
        )
        await runtime.handle_event(
            TranslationEvent(
                event_type=TranslationEventType.PARTIAL_RESULT,
                provider_event_id="draft",
                target_language="en",
                segment_id="translation-1",
                text="Hello",
                begin_time_ms=0,
                end_time_ms=500,
            )
        )
        await runtime.handle_event(
            TranslationEvent(
                event_type=TranslationEventType.PARTIAL_RESULT,
                provider_event_id="draft-throttled",
                target_language="en",
                segment_id="translation-1",
                text="Hello t",
                begin_time_ms=0,
                end_time_ms=600,
            )
        )
        await runtime.handle_event(
            TranslationEvent(
                event_type=TranslationEventType.FINAL_RESULT,
                provider_event_id="blank-final",
                target_language="en",
                segment_id="translation-empty",
                text=" ",
                begin_time_ms=0,
                end_time_ms=700,
            )
        )
        await runtime.handle_event(
            TranslationEvent(
                event_type=TranslationEventType.PARTIAL_RESULT,
                provider_event_id="draft-published",
                target_language="en",
                segment_id="translation-1",
                text="Hello th",
                begin_time_ms=0,
                end_time_ms=800,
            )
        )
        await runtime.handle_event(
            TranslationEvent(
                event_type=TranslationEventType.FINAL_RESULT,
                provider_event_id="final",
                target_language="en",
                segment_id="translation-1",
                text="Hello world",
                begin_time_ms=0,
                end_time_ms=900,
            )
        )
        await runtime.handle_event(
            TranslationEvent(
                event_type=TranslationEventType.PARTIAL_RESULT,
                provider_event_id="late-draft",
                target_language="en",
                segment_id="translation-1",
                text="stale after final",
                begin_time_ms=0,
                end_time_ms=950,
            )
        )

        assert ("publish", "Hello", ()) in operations
        assert ("publish", "Hello t", ()) not in operations
        assert ("publish", "Hello th", ()) in operations
        assert not any(
            operation == ("publish", "stale after final", ())
            for operation in operations
        )
        assert not any(
            operation == ("persist", "stale after final", 5)
            for operation in operations
        )
        assert not any(
            operation == ("persist", " ", 1) for operation in operations
        )
        persist_index = operations.index(("persist", "Hello world", 4))
        commit_index = operations.index("commit", persist_index)
        publish_index = operations.index(
            ("publish", "Hello world", ("source-1",))
        )
        assert persist_index < commit_index < publish_index

    asyncio.run(scenario())
