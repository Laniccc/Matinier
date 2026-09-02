from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COURSE_PLUGIN = ROOT / "plugin-sdk" / "examples" / "course-organizer"
SDK_PYTHON = ROOT / "plugin-sdk" / "python"
for item in (COURSE_PLUGIN, SDK_PYTHON):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from app.plugins.ui_schema import PluginUIViewDocument, parse_plugin_view  # noqa: E402
from course_organizer.models import RealtimeNote  # noqa: E402
from course_organizer.realtime import RealtimeConfig, model_input  # noqa: E402
from course_organizer.session import (  # noqa: E402
    CourseSession, CourseSessionState, _pending_event, _preferred_fragments,
)
from course_organizer.realtime import build_window_evidence  # noqa: E402
from course_organizer.view import (  # noqa: E402
    COURSE_COMMANDS,
    build_course_view,
)


class FakeCapabilities:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object], str | None]] = []
        self.state_value: dict[str, object] | None = None
        self.state_version = 0
        self.views: list[dict[str, object]] = []
        self.state_put_started = asyncio.Event()
        self.state_put_gate = asyncio.Event()
        self.state_put_gate.set()
        self.model_started = asyncio.Event()
        self.model_gate = asyncio.Event()
        self.model_gate.set()
        self.model_failures = 0
        self.note_counter = 0
        self.degraded_saved = asyncio.Event()

    async def capability(
        self,
        *,
        name: str,
        session_scope: str,
        input_value: dict[str, object],
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        self.calls.append((name, input_value, idempotency_key))
        if name == "state.get":
            return {"value": self.state_value, "version": self.state_version}
        if name == "state.put":
            self.state_put_started.set()
            await self.state_put_gate.wait()
            assert input_value["expected_version"] == self.state_version
            self.state_version += 1
            self.state_value = input_value["value"]  # type: ignore[assignment]
            if self.state_value.get("status") == "degraded":
                self.degraded_saved.set()
            return {"version": self.state_version}
        if name == "ui.publish":
            self.views.append(input_value)
            return {"accepted": True, "view_version": input_value["view_version"]}
        if name == "model.invoke":
            self.model_started.set()
            await self.model_gate.wait()
            if self.model_failures:
                self.model_failures -= 1
                raise RuntimeError("model unavailable with secret body")
            payload = input_value["input_payload"]
            assert isinstance(payload, dict)
            items = payload["items"]
            assert isinstance(items, list) and items
            first = items[0]
            assert isinstance(first, dict)
            self.note_counter += 1
            return {
                "output": {
                    "notes": [
                        {
                            "id": f"model-note-{self.note_counter}",
                            "note_type": first["classification"],
                            "title": f"知识笔记 {self.note_counter}",
                            "body": first["text"],
                            "evidence_item_ids": [first["item_id"]],
                            "confidence_status": (
                                "needs_confirmation"
                                if first["classification"] == "needs_confirmation"
                                else "confirmed"
                            ),
                            "language": payload["output_language"],
                            "related_note_ids": [],
                        }
                    ]
                },
                "provider": "fake",
                "model": "fake-course-model",
                "finish_reason": "stop",
                "output_tokens": 20,
            }
        raise AssertionError(f"unexpected capability: {name}")


def task_factory(awaitable, *, name: str):
    return asyncio.create_task(awaitable, name=name)


def test_realtime_model_prompt_declares_the_exact_output_contract() -> None:
    prompt = model_input({}, output_language="zh-CN")["system_prompt"]

    assert isinstance(prompt, str)
    assert '{"notes":[]}' in prompt
    for field in (
        "note_type",
        "evidence_item_ids",
        "confidence_status",
        "related_note_ids",
    ):
        assert field in prompt
    assert "Do not add markdown" in prompt


def media_event(
    sequence: int,
    logical_id: str,
    text: str,
    start_ms: int,
    *,
    duration_ms: int = 2_000,
    revision: int = 1,
    event_type: str = "transcript.final",
    language: str = "en",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": f"event-{sequence}",
        "session_id": "media-1",
        "sequence": sequence,
        "event_type": event_type,
        "media_time_ms": start_ms,
        "duration_ms": duration_ms,
        "logical_id": logical_id,
        "revision": revision,
        "finality": "final",
        "source": "test",
        "payload": {"text": text, "language": language, "confidence": 0.95},
        "created_at": "2026-08-28T00:00:00Z",
    }


def test_host_event_provenance_uses_source_ids_not_logical_envelopes() -> None:
    source = media_event(1, "transcript:source-1", "A gradient means the fastest increase.", 0)
    source["payload"]["evidence"] = {"segment_id": "source-1"}
    translation = media_event(2, "translation:zh-CN:translation-9", "梯度是函数增长最快的方向。", 0,
                              event_type="translation.final", language="zh-CN")
    translation["payload"]["source_segment_ids"] = ["source-1", "source-2"]
    second = media_event(3, "transcript:source-2", "A gradient points in that direction.", 1000)
    evidence = build_window_evidence(_preferred_fragments(
        tuple(_pending_event(event) for event in (source, translation, second)), "zh-CN"))
    assert len(evidence) == 1
    assert next(iter(evidence.values())).source_segment_ids == ("source-1", "source-2")


def test_unaligned_host_translation_does_not_invent_source_provenance() -> None:
    source = media_event(1, "transcript:source-1", "A gradient means the fastest increase.", 0)
    translation = media_event(2, "translation:zh-CN:translation-9", "梯度是函数增长最快的方向。", 0,
                              event_type="translation.final", language="zh-CN")
    translation["payload"]["source_segment_ids"] = []
    evidence = build_window_evidence(_preferred_fragments(
        tuple(_pending_event(event) for event in (source, translation)), "zh-CN"))
    assert {sid for e in evidence.values() for sid in e.source_segment_ids} == {"source-1"}


def test_open_persists_before_ack_and_model_does_not_block_event_or_heartbeat() -> None:
    async def scenario() -> None:
        capabilities = FakeCapabilities()
        capabilities.state_put_gate.clear()
        capabilities.model_gate.clear()
        session = CourseSession(
            capabilities,
            create_task=task_factory,
            config=RealtimeConfig(
                min_window_ms=60_000,
                max_window_ms=90_000,
                trigger_chars=1_600,
                retry_delays_seconds=(2, 10, 30),
                max_notes=500,
            ),
        )
        opened = await session.open(
            {
                "media_session_id": "media-1",
                "session_scope": "scope-course-1",
                "after_sequence": 0,
            }
        )
        assert opened == {"opened": True}
        assert session.state.status == "ready"
        assert capabilities.calls[0][0] == "state.get"
        assert capabilities.views

        pending = asyncio.create_task(
            session.event_batch(
                {
                    "session_scope": "scope-course-1",
                    "events": [
                        media_event(
                            1,
                            "s1",
                            "Gradient is defined as the vector of partial derivatives.",
                            0,
                        ),
                        media_event(
                            2,
                            "s2",
                            "Therefore the update is θ=θ-η∇L.",
                            61_000,
                        ),
                    ],
                }
            )
        )
        await asyncio.wait_for(capabilities.state_put_started.wait(), timeout=1)
        assert not pending.done()
        assert all(name != "model.invoke" for name, _, _ in capabilities.calls)
        capabilities.state_put_gate.set()
        assert await asyncio.wait_for(pending, timeout=1) == {
            "acknowledged_sequence": 2
        }
        await asyncio.wait_for(capabilities.model_started.wait(), timeout=1)
        assert await session.heartbeat({}) == {"ok": True, "status": "processing"}
        # The Event response is already available while the model is still blocked.
        assert not session.background_done

        capabilities.model_gate.set()
        await session.wait_for_idle()
        assert session.state.status == "ready"
        assert len(session.state.notes) == 1
        assert session.state.pending == ()
        names = [name for name, _, _ in capabilities.calls]
        assert names.index("state.put") < names.index("model.invoke")
        assert names.count("ui.publish") >= 2
        await session.close()

    asyncio.run(scenario())


def test_source_translation_revisions_restore_and_replay_are_idempotent() -> None:
    async def scenario() -> None:
        capabilities = FakeCapabilities()
        session = CourseSession(capabilities, create_task=task_factory)
        await session.open(
            {
                "media_session_id": "media-1",
                "session_scope": "scope-course-1",
                "after_sequence": 0,
            }
        )
        events = [
            media_event(1, "s1", "old incomplete text", 0, revision=1),
            media_event(2, "s1", "new final definition of the gradient", 0, revision=2),
            media_event(
                3,
                "s1",
                "梯度是由各个偏导数组成的向量。",
                0,
                revision=2,
                event_type="translation.final",
                language="zh-CN",
            ),
        ]
        assert await session.event_batch(
            {"session_scope": "scope-course-1", "events": events}
        ) == {"acknowledged_sequence": 3}
        assert len(session.state.pending) == 2
        source = next(item for item in session.state.pending if item.event_type == "transcript.final")
        assert source.revision == 2 and source.text.startswith("new final")

        await session.event_batch(
            {"session_scope": "scope-course-1", "events": [events[-1]]}
        )
        assert len(session.state.pending) == 2
        saved = capabilities.state_value
        saved_version = capabilities.state_version
        await session.close()

        capabilities.state_value = saved
        capabilities.state_version = saved_version
        restored = CourseSession(capabilities, create_task=task_factory)
        await restored.open(
            {
                "media_session_id": "media-1",
                "session_scope": "scope-course-2",
                "after_sequence": 3,
            }
        )
        assert restored.state.last_sequence == 3
        assert len(restored.state.pending) == 2
        await restored.event_batch(
            {"session_scope": "scope-course-2", "events": events}
        )
        assert len(restored.state.pending) == 2
        await restored.close()

    asyncio.run(scenario())


def test_model_failure_commits_rule_fallback_and_schedules_bounded_retry() -> None:
    async def scenario() -> None:
        capabilities = FakeCapabilities()
        capabilities.model_failures = 10
        retry_started = asyncio.Event()
        retry_release = asyncio.Event()
        observed_delays: list[float] = []

        async def controlled_sleep(delay: float) -> None:
            observed_delays.append(delay)
            retry_started.set()
            await retry_release.wait()

        session = CourseSession(
            capabilities,
            create_task=task_factory,
            sleep=controlled_sleep,
            config=RealtimeConfig(trigger_chars=10),
        )
        await session.open(
            {
                "media_session_id": "media-1",
                "session_scope": "scope-course-1",
                "after_sequence": 0,
            }
        )
        await session.event_batch(
            {
                "session_scope": "scope-course-1",
                "events": [
                    media_event(
                        1,
                        "s1",
                        "因为目标是凸函数，所以这个局部最优点也是全局最优点。",
                        0,
                        language="zh-CN",
                    )
                ],
            }
        )
        await asyncio.wait_for(capabilities.degraded_saved.wait(), timeout=1)
        await asyncio.wait_for(retry_started.wait(), timeout=1)
        assert session.state.status == "degraded"
        assert session.state.pending == ()
        assert session.state.notes
        assert session.state.notes[0].confidence_status == "rule_fallback"
        assert observed_delays == [2]
        await session.close()

    asyncio.run(scenario())


def test_completed_realtime_task_allows_a_later_window() -> None:
    async def scenario() -> None:
        capabilities = FakeCapabilities()
        session = CourseSession(
            capabilities,
            create_task=task_factory,
            config=RealtimeConfig(trigger_chars=10),
        )
        await session.open(
            {
                "media_session_id": "media-repeat",
                "session_scope": "scope-repeat",
                "after_sequence": 0,
            }
        )
        await session.event_batch(
            {
                "session_scope": "scope-repeat",
                "events": [
                    media_event(1, "s1", "梯度是偏导数组成的向量。", 0, language="zh-CN")
                ],
            }
        )
        await session.wait_for_idle()
        await asyncio.sleep(0)
        assert capabilities.note_counter == 1

        await session.event_batch(
            {
                "session_scope": "scope-repeat",
                "events": [
                    media_event(
                        2,
                        "s2",
                        "例如梯度下降沿反方向更新参数。",
                        61_000,
                        language="zh-CN",
                    )
                ],
            }
        )
        await session.wait_for_idle()
        assert capabilities.note_counter == 2
        await session.close()

    asyncio.run(scenario())


def test_source_language_option_resolves_to_observed_source_language() -> None:
    async def scenario() -> None:
        capabilities = FakeCapabilities()
        session = CourseSession(capabilities, create_task=task_factory)
        await session.open(
            {
                "media_session_id": "media-source-language",
                "session_scope": "scope-source-language",
                "after_sequence": 0,
            }
        )
        await session.event_batch(
            {
                "session_scope": "scope-source-language",
                "events": [
                    media_event(1, "s1", "A gradient is a vector.", 0, language="en-US")
                ],
            }
        )
        assert session.state.source_language == "en-US"
        session.state.output_language = "zh-CN"
        response = await session.command(
            {
                "session_scope": "scope-source-language",
                "command": "set_language",
                "command_id": "command-source-language",
                "values": {"output_language": "source"},
            }
        )
        assert response == {
            "accepted": True,
            "job_id": "command-source-language",
        }
        assert session._language_task is not None
        await asyncio.wait_for(session._language_task, timeout=1)
        assert session.state.output_language == "en-US"
        assert session.state.language_explicit is True
        await session.close()

    asyncio.run(scenario())


def test_multi_hour_state_and_large_document_view_remain_bounded_and_valid() -> None:
    notes = tuple(
        RealtimeNote(
            id=f"note-{index}",
            note_type="knowledge_candidate",
            title=f"知识点 {index}",
            body="课程知识说明" * 30,
            start_ms=index * 60_000,
            end_ms=index * 60_000 + 5_000,
            source_segment_ids=(f"segment-{index}",),
            evidence_item_ids=(f"evidence-{index}",),
            confidence_status="confirmed",
            language="zh-CN",
            related_note_ids=(),
        )
        for index in range(240)  # four hours at one retained note per minute
    )
    state = CourseSessionState(
        last_sequence=10_000,
        state_version=8,
        view_version=12,
        output_language="zh-CN",
        status="ready",
        pending=(),
        notes=notes,
        final_document_markdown="# 最终文档\n" + "非常长的内容" * 30_000,
        history=tuple(
            {"document_id": f"doc-{index}", "label": f"版本 {index}"}
            for index in range(100)
        ),
    )
    encoded = json.dumps(state.to_dict(), ensure_ascii=False).encode("utf-8")
    assert len(encoded) < 2 * 1024 * 1024

    view = build_course_view(state)
    document = PluginUIViewDocument.model_validate(view)
    parsed = parse_plugin_view(view, allowed_commands=COURSE_COMMANDS)
    assert parsed.view_id == "course-organizer"
    assert document.actions
    root = view["root"]
    tabs = next(item for item in root["children"] if item["type"] == "tabs")
    assert [item["label"] for item in tabs["tabs"]] == [
        "实时笔记",
        "最终文档",
        "历史版本",
    ]
    serialized = json.dumps(view, ensure_ascii=False)
    assert "请使用可信文档下载" in serialized
    assert len(serialized) < 64_000
