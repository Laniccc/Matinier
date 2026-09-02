from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
COURSE_PLUGIN = ROOT / "plugin-sdk" / "examples" / "course-organizer"
if str(COURSE_PLUGIN) not in sys.path:
    sys.path.insert(0, str(COURSE_PLUGIN))

from course_organizer.evidence import (  # noqa: E402
    parse_knowledge_items,
    parse_realtime_notes,
)
from course_organizer.filtering import filter_fragments  # noqa: E402
from course_organizer.models import (  # noqa: E402
    FINAL_CATEGORIES,
    CourseCategory,
    CourseDocument,
    EvidenceItem,
    KnowledgeItem,
    RealtimeNote,
    TranscriptFragment,
)
from course_organizer.prompts import final_categories_prompt  # noqa: E402


def fragment(
    segment_id: str,
    text: str,
    start_ms: int,
    end_ms: int,
    *,
    revision: int = 1,
    language: str = "zh-CN",
    confidence: float | None = 0.95,
) -> TranscriptFragment:
    return TranscriptFragment(
        segment_id=segment_id,
        revision=revision,
        text=text,
        start_ms=start_ms,
        end_ms=end_ms,
        language=language,
        confidence=confidence,
    )


def test_revision_replacement_stutter_merge_and_filler_drop() -> None:
    spans = filter_fragments(
        (
            fragment("s1", "旧版本里的定义不完整……", 0, 2_000, revision=1),
            fragment("s1", "梯度是函数增长最快方向上的向量。", 0, 3_000, revision=2),
            fragment("s2", "梯度是函数增长最快方向上的向量。", 3_100, 5_000),
            fragment("s3", "所以所以方向导数等于梯度与方向向量的点积。", 5_100, 8_000),
            fragment("s4", "嗯，那个，啊。", 8_100, 9_000),
        )
    )

    assert len(spans) == 2
    assert "旧版本" not in " ".join(item.text for item in spans)
    assert spans[0].source_segment_ids == ("s1", "s2")
    assert spans[0].start_ms == 0 and spans[0].end_ms == 5_000
    assert spans[0].classification == "knowledge_candidate"
    assert spans[1].text.startswith("所以方向导数")
    assert spans[1].source_segment_ids == ("s3",)
    assert all(item.start_ms <= item.end_ms for item in spans)


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (
            fragment("greet", "大家好，先确认播放器音量，稍后开始第三章。", 0, 2_000),
            {"transition", "background"},
        ),
        (
            fragment("ad", "Use coupon SAVE20 on the platform before continuing the lesson.", 0, 2_000, language="en"),
            {"background"},
        ),
        (
            fragment("admin", "作业周五提交，本节课考勤方式稍后公布。", 0, 2_000),
            {"transition", "background"},
        ),
        (
            fragment("definition", "梯度是由各个偏导数组成的向量，它描述局部增长方向。", 0, 3_000),
            {"knowledge_candidate"},
        ),
        (
            fragment("cause", "因为损失函数是凸函数，所以任意局部最优点也是全局最优点。", 0, 3_000),
            {"knowledge_candidate"},
        ),
        (
            fragment("compare", "Unlike batch descent, stochastic descent updates after one sample, whereas batch descent uses all samples.", 0, 4_000, language="en"),
            {"knowledge_candidate"},
        ),
        (
            fragment("steps", "第一步计算梯度，第二步乘以学习率，最后更新参数。", 0, 4_000),
            {"knowledge_candidate"},
        ),
        (
            fragment("formula", "更新式为 θ(t+1)=θ(t)-η∇L，η=0.01。", 0, 3_000),
            {"knowledge_candidate"},
        ),
        (
            fragment("example", "举个例子：设输入为二乘二矩阵，我们逐行演示卷积计算。", 0, 4_000),
            {"example"},
        ),
        (
            fragment("uncertain", "这部分可能是……但是前后数值对不上", 0, 2_000, confidence=0.35),
            {"needs_confirmation"},
        ),
        (
            # Background context wins even though the UI label happens to contain “定义”.
            fragment("priority", "课程平台按钮名叫“定义”，请点击它切换下一页。", 0, 2_000),
            {"background"},
        ),
    ),
)
def test_multilingual_contextual_classification(
    value: TranscriptFragment,
    expected: set[str],
) -> None:
    spans = filter_fragments((value,))
    assert len(spans) == 1
    assert spans[0].classification in expected


def evidence() -> dict[str, EvidenceItem]:
    return {
        "e1": EvidenceItem(
            item_id="e1",
            text="梯度是函数增长最快方向上的向量。",
            source_segment_ids=("s1",),
            start_ms=1_000,
            end_ms=2_500,
            confirmation_status="supported",
        ),
        "e2": EvidenceItem(
            item_id="e2",
            text="这里的符号可能与上一页冲突。",
            source_segment_ids=("s2",),
            start_ms=2_500,
            end_ms=4_000,
            confirmation_status="needs_confirmation",
        ),
    }


def test_model_realtime_output_is_immutable_and_evidence_closed() -> None:
    notes = parse_realtime_notes(
        {
            "notes": [
                {
                    "id": "note-1",
                    "note_type": "knowledge_candidate",
                    "title": "梯度",
                    "body": "梯度描述函数局部增长最快的方向。",
                    "evidence_item_ids": ["e1"],
                    "confidence_status": "confirmed",
                    "language": "zh-CN",
                    "related_note_ids": [],
                }
            ]
        },
        allowed_evidence=evidence(),
    )
    assert notes == (
        RealtimeNote(
            id="note-1",
            note_type="knowledge_candidate",
            title="梯度",
            body="梯度描述函数局部增长最快的方向。",
            start_ms=1_000,
            end_ms=2_500,
            source_segment_ids=("s1",),
            evidence_item_ids=("e1",),
            confidence_status="confirmed",
            language="zh-CN",
            related_note_ids=(),
        ),
    )
    with pytest.raises((AttributeError, TypeError)):
        notes[0].title = "mutated"  # type: ignore[misc]


@pytest.mark.parametrize(
    "mutator",
    (
        lambda item: item.update(id="note-1"),
        lambda item: item.update(evidence_item_ids=[]),
        lambda item: item.update(evidence_item_ids=["outside-current-chunk"]),
        lambda item: item.update(note_type="invented"),
        lambda item: item.update(confidence_status="certain-enough"),
        lambda item: item.update(evidence_item_ids=["e2"], confidence_status="confirmed"),
    ),
)
def test_model_realtime_output_fails_closed(mutator) -> None:
    first = {
        "id": "note-1",
        "note_type": "knowledge_candidate",
        "title": "梯度",
        "body": "正文",
        "evidence_item_ids": ["e1"],
        "confidence_status": "confirmed",
        "language": "zh-CN",
        "related_note_ids": [],
    }
    second = dict(first, id="note-2")
    mutator(second)
    with pytest.raises(ValueError):
        parse_realtime_notes(
            {"notes": [first, second]},
            allowed_evidence=evidence(),
        )


def test_knowledge_items_categories_relations_and_course_document() -> None:
    raw = {
        "items": [
            {
                "id": "knowledge-1",
                "category": "核心概念",
                "topic_path": ["优化", "梯度"],
                "title": "梯度",
                "statement": "梯度描述局部增长最快方向。",
                "explanation": "由偏导数组成。",
                "evidence_item_ids": ["e1"],
                "related_item_ids": [],
                "confirmation_status": "confirmed",
            },
            {
                "id": "knowledge-2",
                "category": "易错点与待确认问题",
                "topic_path": ["优化", "符号"],
                "title": "符号冲突",
                "statement": "课件符号可能存在冲突。",
                "explanation": "需要对照上一页确认。",
                "evidence_item_ids": ["e2"],
                "related_item_ids": ["knowledge-1"],
                "confirmation_status": "needs_confirmation",
            },
        ]
    }
    items = parse_knowledge_items(raw, allowed_evidence=evidence())
    assert items[0].source_segment_ids == ("s1",)
    assert items[0].time_ranges == ((1_000, 2_500),)
    assert items[1].related_item_ids == ("knowledge-1",)
    assert isinstance(items[0], KnowledgeItem)

    categories = tuple(
        CourseCategory(
            name=name,
            items=tuple(item for item in items if item.category == name),
        )
        for name in FINAL_CATEGORIES
    )
    document = CourseDocument(
        title="课程内容整理",
        language="zh-CN",
        realtime_notes=(),
        categories=categories,
        warnings=("符号需要确认",),
    )
    assert tuple(item.name for item in document.categories) == FINAL_CATEGORIES
    prompt = final_categories_prompt()
    assert all(prompt.count(name) == 1 for name in FINAL_CATEGORIES)


@pytest.mark.parametrize(
    "change",
    (
        {"category": "其他"},
        {"evidence_item_ids": []},
        {"evidence_item_ids": ["outside-current-chunk"]},
        {"confirmation_status": "probably"},
        {"related_item_ids": ["missing-related-item"]},
        {"evidence_item_ids": ["e2"], "confirmation_status": "confirmed"},
    ),
)
def test_knowledge_model_output_rejects_unsupported_facts(change) -> None:
    item = {
        "id": "knowledge-1",
        "category": "核心概念",
        "topic_path": ["优化"],
        "title": "梯度",
        "statement": "梯度描述局部增长最快方向。",
        "explanation": "由偏导数组成。",
        "evidence_item_ids": ["e1"],
        "related_item_ids": [],
        "confirmation_status": "confirmed",
    }
    item.update(change)
    with pytest.raises(ValueError):
        parse_knowledge_items({"items": [item]}, allowed_evidence=evidence())


def test_domain_shapes_reject_reversed_ranges_and_duplicate_categories() -> None:
    with pytest.raises(ValueError, match="range"):
        EvidenceItem(
            item_id="bad",
            text="bad",
            source_segment_ids=("s1",),
            start_ms=20,
            end_ms=10,
            confirmation_status="supported",
        )
    with pytest.raises(ValueError, match="categories"):
        CourseDocument(
            title="课程内容整理",
            language="zh-CN",
            realtime_notes=(),
            categories=(
                CourseCategory(name="核心概念", items=()),
                CourseCategory(name="核心概念", items=()),
            ),
            warnings=(),
        )
