from __future__ import annotations

import re
from difflib import SequenceMatcher

from .models import ClassifiedSpan, TranscriptFragment


_PUNCTUATION = re.compile(r"[\s\u3000,，。.!！?？:：;；、'\"“”‘’()（）\[\]【】]+")
_FILLER = re.compile(
    r"^(?:(?:嗯+|呃+|啊+|那个|然后|就是|这个|okay|ok|um+|uh+|you know)[,，。.!！?？\s]*)+$",
    re.IGNORECASE,
)
_BACKGROUND = re.compile(
    r"(?:平台|播放器|按钮|点击|下一页|音量|优惠|折扣|广告|赞助|coupon|subscribe|"
    r"player|click|button|next page)",
    re.IGNORECASE,
)
_GREETING_OR_ADMIN = re.compile(
    r"(?:大家好|同学们好|欢迎|开始上课|休息|作业|提交|考勤|考试安排|"
    r"hello everyone|welcome|homework|office hours|attendance|deadline)",
    re.IGNORECASE,
)
_EXAMPLE = re.compile(
    r"(?:举个?例|例如|演示|故事|题目|假设输入|for example|example|demo|"
    r"consider the problem|suppose we)",
    re.IGNORECASE,
)
_DEFINITION = re.compile(
    r"(?:.{1,30}(?:是指|定义为|可以定义为|意味着).{4,}|"
    r"(?:^|[。！？])[\u4e00-\u9fffA-Za-z0-9∇θλ]{1,24}是[^。！？]{8,}(?:[。！？]|$)|"
    r".{1,40}\bis defined as\b.{4,}|.{1,40}\bmeans\b.{4,})",
    re.IGNORECASE,
)
_CAUSE = re.compile(
    r"(?:因为.{2,}所以|由于.{2,}因此|\bbecause\b.{3,}\b(?:therefore|so|hence)\b)",
    re.IGNORECASE,
)
_COMPARE = re.compile(
    r"(?:与.{1,20}(?:相比|不同)|一方面.{2,}另一方面|"
    r"\bunlike\b.{3,}\bwhereas\b|\bcompared with\b)",
    re.IGNORECASE,
)
_PROCEDURE = re.compile(
    r"(?:(?:第一步|首先).{2,}(?:第二步|然后|接着).{2,}(?:最后|最终)|"
    r"\b(?:first|step one)\b.{2,}\b(?:second|then|next)\b.{2,}\b(?:finally|last)\b)",
    re.IGNORECASE,
)
_FORMULA = re.compile(r"(?:[=≈<>≤≥∑∇θλ]|\b\w+\s*=\s*-?\d|\d+(?:\.\d+)?%)")
_CONCLUSION_OR_EMPHASIS = re.compile(
    r"(?:因此可得|综上|结论是|务必注意|关键在于|记住|"
    r"therefore we conclude|the key point|remember that)",
    re.IGNORECASE,
)
_UNCERTAIN = re.compile(
    r"(?:不确定|可能是|似乎|有冲突|对不上|需要确认|"
    r"not sure|might be|seems|contradict|needs confirmation)",
    re.IGNORECASE,
)


def filter_fragments(
    fragments: tuple[TranscriptFragment, ...],
    *,
    merge_gap_ms: int = 15_000,
) -> tuple[ClassifiedSpan, ...]:
    if merge_gap_ms < 0:
        raise ValueError("merge gap must not be negative")
    latest: dict[str, TranscriptFragment] = {}
    for fragment in fragments:
        existing = latest.get(fragment.segment_id)
        if existing is None or fragment.revision > existing.revision:
            latest[fragment.segment_id] = fragment
    ordered = sorted(
        latest.values(),
        key=lambda item: (item.start_ms, item.end_ms, item.segment_id),
    )
    output: list[ClassifiedSpan] = []
    for fragment in ordered:
        text = _remove_stutter(fragment.text.strip())
        if _is_filler(text):
            continue
        classification = _classify(fragment, text)
        confidence_status = (
            "needs_confirmation"
            if classification == "needs_confirmation"
            else "rule_fallback"
        )
        candidate = ClassifiedSpan(
            classification=classification,
            text=text,
            start_ms=fragment.start_ms,
            end_ms=fragment.end_ms,
            source_segment_ids=fragment.source_segment_ids or (fragment.segment_id,),
            language=fragment.language,
            confidence_status=confidence_status,
        )
        if output and _can_merge(output[-1], candidate, merge_gap_ms):
            previous = output[-1]
            output[-1] = ClassifiedSpan(
                classification=previous.classification,
                text=_longer_text(previous.text, candidate.text),
                start_ms=min(previous.start_ms, candidate.start_ms),
                end_ms=max(previous.end_ms, candidate.end_ms),
                source_segment_ids=tuple(dict.fromkeys((
                    *previous.source_segment_ids,
                    *candidate.source_segment_ids,
                ))),
                language=previous.language,
                confidence_status=(
                    "needs_confirmation"
                    if "needs_confirmation"
                    in {previous.confidence_status, candidate.confidence_status}
                    else "rule_fallback"
                ),
            )
        else:
            output.append(candidate)
    return tuple(output)


def _classify(fragment: TranscriptFragment, text: str) -> str:
    if (
        fragment.confidence is not None
        and fragment.confidence < 0.55
        or text.endswith(("…", "..."))
        or _UNCERTAIN.search(text)
    ):
        return "needs_confirmation"
    if _BACKGROUND.search(text):
        return "background"
    if _GREETING_OR_ADMIN.search(text):
        return "transition"
    if _EXAMPLE.search(text):
        return "example"
    structural_knowledge = any(
        pattern.search(text)
        for pattern in (
            _DEFINITION,
            _CAUSE,
            _COMPARE,
            _PROCEDURE,
            _FORMULA,
            _CONCLUSION_OR_EMPHASIS,
        )
    )
    if structural_knowledge and len(_canonical(text)) >= 10:
        return "knowledge_candidate"
    return "transition"


def _remove_stutter(text: str) -> str:
    value = re.sub(r"\b([A-Za-z]{1,30})(?:\s+\1\b)+", r"\1", text, flags=re.I)
    value = re.sub(r"^(所以|然后|就是|这个|那么)\1+", r"\1", value)
    return value.strip()


def _is_filler(text: str) -> bool:
    if not text or _FILLER.fullmatch(text):
        return True
    canonical = _canonical(text)
    return canonical in {"嗯", "呃", "啊", "那个", "um", "uh", "okay", "ok"}


def _canonical(text: str) -> str:
    return _PUNCTUATION.sub("", text).casefold()


def _can_merge(
    previous: ClassifiedSpan,
    current: ClassifiedSpan,
    merge_gap_ms: int,
) -> bool:
    if (
        current.start_ms - previous.end_ms > merge_gap_ms
        or previous.language != current.language
        or previous.classification != current.classification
    ):
        return False
    left = _canonical(previous.text)
    right = _canonical(current.text)
    return left == right or SequenceMatcher(None, left, right).ratio() >= 0.94


def _longer_text(first: str, second: str) -> str:
    return first if len(_canonical(first)) >= len(_canonical(second)) else second


__all__ = ["filter_fragments"]
