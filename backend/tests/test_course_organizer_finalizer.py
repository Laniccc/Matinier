from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
COURSE_PLUGIN = ROOT / "plugin-sdk" / "examples" / "course-organizer"
SDK_PYTHON = ROOT / "plugin-sdk" / "python"
for item in (COURSE_PLUGIN, SDK_PYTHON):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from course_organizer.document import (  # noqa: E402
    build_course_document,
    course_document_content,
    render_course_markdown,
)
from course_organizer.finalizer import CourseFinalizer  # noqa: E402
from course_organizer.language import (  # noqa: E402
    choose_document_language,
    localize_notes,
    validate_language,
)
from course_organizer.models import KnowledgeItem, RealtimeNote  # noqa: E402
from course_organizer.prompts import final_categories_prompt  # noqa: E402
from course_organizer.realtime import RealtimeConfig  # noqa: E402
from course_organizer.session import CourseSession  # noqa: E402


def realtime_note(note_id: str = "note-1", *, language: str = "zh-CN") -> RealtimeNote:
    return RealtimeNote(
        id=note_id,
        note_type="knowledge_candidate",
        title="梯度",
        body="梯度描述函数增长最快的方向。",
        start_ms=1_000,
        end_ms=2_500,
        source_segment_ids=("s1",),
        evidence_item_ids=("realtime-local",),
        confidence_status="confirmed",
        language=language,
        related_note_ids=(),
    )


def package_evidence(item_id: str, segment_id: str, start_ms: int) -> dict[str, object]:
    return {
        "document_id": "evidence-document",
        "document_kind": "evidence_index",
        "language": None,
        "document_content_hash": "a" * 64,
        "item_id": item_id,
        "source_segment_ids": [segment_id],
        "start_ms": start_ms,
        "end_ms": start_ms + 1_500,
        "raw_text": f"raw {item_id}",
        "effective_text": f"知识内容 {item_id}",
        "source_document_id": "source-document",
    }


class FinalizationCapabilities:
    def __init__(self, *, pages: list[list[dict[str, object]]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object], str | None]] = []
        self.pages = pages or [
            [package_evidence("e1", "s1", 1_000)],
            [package_evidence("e2", "s2", 3_000)],
        ]
        self.prepare_gate = asyncio.Event()
        self.prepare_gate.set()
        self.invalid_map_responses = 0
        self.fail_all_models = False
        self.publications: list[dict[str, object]] = []
        self.document_versions: dict[str, int] = {}
        self.state_value: dict[str, object] | None = None
        self.state_version = 0
        self.views: list[dict[str, object]] = []
        self.model_tasks: list[str] = []

    async def capability(
        self,
        *,
        name: str,
        session_scope: str,
        input_value: dict[str, object],
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        self.calls.append((name, copy.deepcopy(input_value), idempotency_key))
        if name == "state.get":
            return {"value": self.state_value, "version": self.state_version}
        if name == "state.put":
            assert input_value["expected_version"] == self.state_version
            self.state_version += 1
            self.state_value = copy.deepcopy(input_value["value"])
            return {"version": self.state_version}
        if name == "ui.publish":
            self.views.append(copy.deepcopy(input_value))
            return {"accepted": True, "view_version": input_value["view_version"]}
        if name == "delivery.prepare":
            await self.prepare_gate.wait()
            return {
                "package_id": "package-1",
                "package_version": len(self.publications) + 1,
                "content_hash": "b" * 64,
                "source_language": "en",
                "target_languages": ["zh-CN"],
                "final_sequence": input_value["final_sequence"],
            }
        if name == "delivery.query":
            cursor = int(input_value["after_item"])
            page_index = cursor
            page = self.pages[page_index]
            return {
                "package_id": "package-1",
                "package_version": 1,
                "content_hash": "b" * 64,
                "items": page,
                "next_after_item": page_index + 1 if page_index + 1 < len(self.pages) else None,
            }
        if name == "model.invoke":
            payload = input_value["input_payload"]
            assert isinstance(payload, dict)
            task = str(payload["task"])
            self.model_tasks.append(task)
            if self.fail_all_models:
                raise RuntimeError("provider unavailable secret")
            if task == "course.localize_notes":
                items = payload["items"]
                return {
                    "output": {
                        "notes": [
                            {
                                "id": item["id"],
                                "title": f"localized {item['title']}",
                                "body": f"localized {item['body']}",
                            }
                            for item in items
                        ]
                    },
                    "provider": "fake",
                    "model": "fake",
                    "output_tokens": 10,
                }
            evidence_items = payload.get("items", [])
            if task.startswith("course.final.map"):
                if self.invalid_map_responses:
                    self.invalid_map_responses -= 1
                    return {"output": {"items": [{"id": "invalid"}]}, "provider": "fake", "model": "fake"}
                return {
                    "output": {
                        "items": [self._knowledge(item, index) for index, item in enumerate(evidence_items)]
                    },
                    "provider": "fake",
                    "model": "fake",
                    "output_tokens": 20,
                }
            if task.startswith("course.final.reduce"):
                return {
                    "output": {"items": evidence_items},
                    "provider": "fake",
                    "model": "fake",
                    "output_tokens": 20,
                }
            if task == "course.realtime_notes":
                first = evidence_items[0]
                return {
                    "output": {
                        "notes": [
                            {
                                "id": "terminal-realtime-note",
                                "note_type": first["classification"],
                                "title": "终态前刷新",
                                "body": first["text"],
                                "evidence_item_ids": [first["item_id"]],
                                "confidence_status": "confirmed",
                                "language": payload["output_language"],
                                "related_note_ids": [],
                            }
                        ]
                    },
                    "provider": "fake",
                    "model": "fake",
                }
        if name == "document.publish":
            self.publications.append(copy.deepcopy(input_value))
            identity = str(input_value["identity_key"])
            version = self.document_versions.get(identity, 0) + 1
            self.document_versions[identity] = version
            return {
                "document_id": f"document-{identity}-{version}",
                "document_version": version,
                "identity_key": identity,
                "content_hash": f"{version:064x}",
                "language": input_value["language"],
                "trigger": input_value["trigger"],
                "completeness": input_value["completeness"],
            }
        raise AssertionError(f"unexpected capability {name}")

    @staticmethod
    def _knowledge(item: dict[str, object], index: int) -> dict[str, object]:
        return {
            "id": f"knowledge-{item['item_id']}-{index}",
            "category": "核心概念",
            "topic_path": ["课程", "基础"],
            "title": f"知识点 {item['item_id']}",
            "statement": item["text"],
            "explanation": "由课程证据直接整理。",
            "evidence_item_ids": [item["item_id"]],
            "related_item_ids": [],
            "confirmation_status": "confirmed",
        }


def task_factory(awaitable, *, name: str):
    return asyncio.create_task(awaitable, name=name)


def test_final_model_prompt_declares_exact_map_reduce_contract() -> None:
    prompt = final_categories_prompt()

    assert '{"items":[]}' in prompt
    for field in (
        "category",
        "topic_path",
        "evidence_item_ids",
        "related_item_ids",
        "confirmation_status",
    ):
        assert field in prompt
    assert "Do not add markdown" in prompt


def test_finalizer_bounds_both_model_stages_without_losing_evidence() -> None:
    class ReusedIds(FinalizationCapabilities):
        @staticmethod
        def _knowledge(item, index):
            result = FinalizationCapabilities._knowledge(item, index)
            result["id"] = f"point-{index}"
            return result

    async def scenario():
        capabilities = ReusedIds(pages=[[
            package_evidence(f"e{i}", f"s{i}", i * 2000) for i in range(18)
        ]])
        result = await CourseFinalizer(
            capabilities, session_scope="scope", output_language="zh-CN", realtime_notes=(),
        ).run(job_id="bounded", trigger="session_completed", final_sequence=18)
        calls = [value for name, value, _key in capabilities.calls if name == "model.invoke"]
        assert len(calls) == 10
        assert all(len(call["input_payload"]["items"]) <= 4 for call in calls)
        assert all(call["max_output_tokens"] == 4096 for call in calls)
        published = capabilities.publications[0]
        assert {ref["item_id"] for ref in published["evidence_refs"]} == {f"e{i}" for i in range(18)}
        assert result.completeness == "complete"
        return result.content

    assert asyncio.run(scenario()) == asyncio.run(scenario())


def test_terminal_retry_preserves_complete_semantics() -> None:
    async def scenario():
        capabilities = FinalizationCapabilities()
        capabilities.fail_all_models = True
        session = CourseSession(capabilities, create_task=task_factory, final_retry_delays=())
        await session.open({"media_session_id": "media", "session_scope": "scope", "after_sequence": 0})
        await session.event_batch({"session_scope": "scope", "events": [event(1, "session.completed")]})
        await session.wait_for_terminal_idle()
        assert session.state.final_status == "waiting_retry"
        capabilities.fail_all_models = False
        accepted = await session.command({"session_scope": "scope", "command": "retry_final",
                                          "command_id": "retry-terminal", "values": {}})
        assert accepted["job_id"] == "retry-terminal"
        await session.wait_for_terminal_idle()
        assert capabilities.publications[-1]["trigger"] == "session_completed"
        assert capabilities.publications[-1]["completeness"] == "complete"
        assert session.state.terminal_job_id is None
        await session.close()
    asyncio.run(scenario())


def event(sequence: int, event_type: str, *, text: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {}
    if text is not None:
        payload = {"text": text, "language": "zh-CN", "confidence": 0.95}
    return {
        "sequence": sequence,
        "event_type": event_type,
        "logical_id": f"logical-{sequence}",
        "revision": 1,
        "media_time_ms": sequence * 1_000,
        "duration_ms": 900,
        "payload": payload,
    }


def test_manual_generation_returns_first_job_and_publishes_closed_two_part_document() -> None:
    async def scenario() -> None:
        capabilities = FinalizationCapabilities()
        capabilities.prepare_gate.clear()
        capabilities.state_value = {
            "schema_version": 1,
            "last_sequence": 8,
            "view_version": 2,
            "output_language": "zh-CN",
            "status": "ready",
            "pending": [],
            "notes": [
                {
                    "id": realtime_note().id,
                    "note_type": realtime_note().note_type,
                    "title": realtime_note().title,
                    "body": realtime_note().body,
                    "start_ms": realtime_note().start_ms,
                    "end_ms": realtime_note().end_ms,
                    "source_segment_ids": ["s1"],
                    "evidence_item_ids": ["realtime-local"],
                    "confidence_status": "confirmed",
                    "language": "zh-CN",
                    "related_note_ids": [],
                }
            ],
            "final_document_markdown": "",
            "history": [],
        }
        session = CourseSession(capabilities, create_task=task_factory)
        await session.open(
            {"media_session_id": "media-1", "session_scope": "scope-1", "after_sequence": 8}
        )
        first = await session.command(
            {"session_scope": "scope-1", "command": "generate_final", "command_id": "cmd-1", "values": {}}
        )
        second = await session.command(
            {"session_scope": "scope-1", "command": "generate_final", "command_id": "cmd-2", "values": {}}
        )
        assert first == second == {"accepted": True, "job_id": "cmd-1"}

        # Final generation is blocked, but realtime event handling remains available.
        assert await session.event_batch(
            {
                "session_scope": "scope-1",
                "events": [event(9, "transcript.final", text="短的后续字幕")],
            }
        ) == {"acknowledged_sequence": 9}
        capabilities.prepare_gate.set()
        await session.wait_for_final_idle()

        prepare = next(call for call in capabilities.calls if call[0] == "delivery.prepare")
        assert prepare[1]["trigger"] == "manual"
        assert prepare[2] == "course-delivery:cmd-1"
        queries = [call for call in capabilities.calls if call[0] == "delivery.query"]
        assert [call[1]["after_item"] for call in queries] == [0, 1]
        assert capabilities.model_tasks.count("course.final.map") == 2
        assert capabilities.model_tasks.count("course.final.reduce") == 1

        assert len(capabilities.publications) == 1
        published = capabilities.publications[0]
        assert published["identity_key"] == "course-notes:zh-CN"
        assert published["completeness"] == "interim"
        assert published["trigger"] == "manual"
        assert {item["item_id"] for item in published["evidence_refs"]} == {"e1", "e2"}
        assert [part["id"] for part in published["content"]["parts"]] == [
            "realtime-notes",
            "knowledge",
        ]
        markdown = published["markdown"]
        assert markdown.count("\n# ") + markdown.startswith("# ") == 2
        assert "# 第一部分：实时知识笔记" in markdown
        assert "# 第二部分：课程知识点整理" in markdown
        assert "00:00:01.000" in markdown
        assert session.state.history[0]["completeness"] == "interim"
        await session.close()

    asyncio.run(scenario())


def test_terminal_flushes_short_window_and_restart_reuses_deterministic_job() -> None:
    async def scenario() -> None:
        capabilities = FinalizationCapabilities()
        capabilities.prepare_gate.clear()
        first = CourseSession(
            capabilities,
            create_task=task_factory,
            config=RealtimeConfig(trigger_chars=10_000),
        )
        await first.open(
            {"media_session_id": "media-1", "session_scope": "scope-1", "after_sequence": 0}
        )
        await first.event_batch(
            {
                "session_scope": "scope-1",
                "events": [
                    event(1, "transcript.final", text="梯度是偏导数组成的向量。"),
                    event(2, "session.completed"),
                ],
            }
        )
        await asyncio.sleep(0)
        saved_job = first.state.terminal_job_id
        assert saved_job is not None
        saved_state = copy.deepcopy(capabilities.state_value)
        saved_version = capabilities.state_version
        await first.close()

        capabilities.state_value = saved_state
        capabilities.state_version = saved_version
        restored = CourseSession(capabilities, create_task=task_factory)
        await restored.open(
            {"media_session_id": "media-1", "session_scope": "scope-2", "after_sequence": 2}
        )
        assert restored.state.terminal_job_id == saved_job
        capabilities.prepare_gate.set()
        await restored.wait_for_terminal_idle()
        assert "course.realtime_notes" in capabilities.model_tasks
        assert len(capabilities.publications) == 1
        assert capabilities.publications[0]["completeness"] == "complete"
        terminal_prepares = [call for call in capabilities.calls if call[0] == "delivery.prepare"]
        assert terminal_prepares
        assert all(call[2] == f"course-delivery:{saved_job}" for call in terminal_prepares)
        assert restored.state.terminal_job_id is None
        await restored.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("terminal_type", ("session.failed", "session.cancelled"))
def test_partial_terminal_without_final_shows_stable_reason_and_no_empty_document(
    terminal_type: str,
) -> None:
    async def scenario() -> None:
        capabilities = FinalizationCapabilities(pages=[])

        async def missing_final(**kwargs):
            if kwargs["name"] == "delivery.prepare":
                raise ValueError("Session has no Final captions and internal path C:/secret")
            return await FinalizationCapabilities.capability(capabilities, **kwargs)

        capabilities.capability = missing_final  # type: ignore[method-assign]
        session = CourseSession(capabilities, create_task=task_factory, final_retry_delays=())
        await session.open(
            {"media_session_id": "media-1", "session_scope": "scope-1", "after_sequence": 0}
        )
        await session.event_batch(
            {"session_scope": "scope-1", "events": [event(1, terminal_type)]}
        )
        await session.wait_for_terminal_idle()
        assert capabilities.publications == []
        assert session.state.final_status == "waiting_retry"
        assert session.state.final_error == "当前课程没有可用于最终整理的字幕。"
        assert "secret" not in session.state.final_error
        await session.close()

    asyncio.run(scenario())


def test_language_selection_localizes_in_chunks_and_swaps_only_after_success() -> None:
    async def scenario() -> None:
        capabilities = FinalizationCapabilities()
        notes = tuple(realtime_note(f"note-{index}") for index in range(5))
        localized = await localize_notes(
            capabilities,
            session_scope="scope-1",
            notes=notes,
            target_language="en-US",
            chunk_size=2,
        )
        assert len([task for task in capabilities.model_tasks if task == "course.localize_notes"]) == 3
        assert all(item.language == "en-US" for item in localized)
        assert all(item.title.startswith("localized") for item in localized)
        assert all(item.language == "zh-CN" for item in notes)

        capabilities.fail_all_models = True
        before = copy.deepcopy(localized)
        with pytest.raises(RuntimeError):
            await localize_notes(
                capabilities,
                session_scope="scope-1",
                notes=localized,
                target_language="fr",
                chunk_size=2,
            )
        assert localized == before

    asyncio.run(scenario())
    assert choose_document_language("en", ("zh-CN",), None) == "zh-CN"
    assert choose_document_language("en", (), None) == "en"
    assert choose_document_language("en", ("zh-CN",), "ja-JP") == "ja-JP"
    with pytest.raises(ValueError):
        validate_language("../../secret")


def test_invalid_model_evidence_keeps_old_document_and_one_repair_is_allowed() -> None:
    async def scenario() -> None:
        capabilities = FinalizationCapabilities()
        capabilities.invalid_map_responses = 1
        finalizer = CourseFinalizer(
            capabilities,
            session_scope="scope-1",
            output_language="zh-CN",
            realtime_notes=(realtime_note(),),
        )
        result = await finalizer.run(job_id="repair-job", trigger="manual", final_sequence=9)
        assert result.document_version == 1
        assert capabilities.model_tasks[:2] == [
            "course.final.map",
            "course.final.map_repair",
        ]

        capabilities.invalid_map_responses = 2
        old_publication = copy.deepcopy(capabilities.publications[-1])
        with pytest.raises(ValueError):
            await finalizer.run(job_id="bad-job", trigger="manual", final_sequence=10)
        assert capabilities.publications[-1] == old_publication

    asyncio.run(scenario())


def test_document_builder_has_exact_two_parts_and_package_evidence_only() -> None:
    knowledge = KnowledgeItem(
        id="knowledge-1",
        category="核心概念",
        topic_path=("优化", "梯度"),
        title="梯度",
        statement="梯度描述局部增长最快方向。",
        explanation="由偏导数组成。",
        evidence_item_ids=("e1",),
        source_segment_ids=("s1",),
        time_ranges=((1_000, 2_500),),
        related_item_ids=(),
        confirmation_status="confirmed",
    )
    document = build_course_document(
        language="zh-CN",
        realtime_notes=(realtime_note(),),
        knowledge_items=(knowledge,),
        warnings=(),
    )
    content = course_document_content(document)
    markdown = render_course_markdown(document)
    assert [item["title"] for item in content["parts"]] == [
        "第一部分：实时知识笔记",
        "第二部分：课程知识点整理",
    ]
    assert [line for line in markdown.splitlines() if line.startswith("# ")] == [
        "# 第一部分：实时知识笔记",
        "# 第二部分：课程知识点整理",
    ]
    assert "00:00:01.000–00:00:02.500" in markdown
