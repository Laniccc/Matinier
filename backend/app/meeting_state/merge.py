from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from app.meeting_state.contracts import (
    EntityProposal,
    MeetingStateDelta,
    StateItemProposal,
)
from app.meeting_state.models import (
    ActionCandidate,
    Conflict,
    Decision,
    Entity,
    Highlight,
    MeetingState,
    MeetingStateItem,
    Topic,
)


def _normalize_content(value: object) -> object:
    if isinstance(value, str):
        return " ".join(value.casefold().split())
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_content(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize_content(item) for item in value]
    return value


def stable_item_id(
    *,
    kind: str,
    content: object,
    evidence_ids: Sequence[str],
) -> str:
    canonical = json.dumps(
        {
            "kind": kind,
            "content": _normalize_content(content),
            "evidence_ids": sorted(set(evidence_ids)),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"msi_{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _state_item(
    kind: str,
    proposal: StateItemProposal,
    source_segment_revisions: Mapping[str, int],
) -> MeetingStateItem:
    item_id = stable_item_id(
        kind=kind,
        content=proposal.text,
        evidence_ids=proposal.source_segment_ids,
    )
    values = {
        "item_id": item_id,
        "text": proposal.text,
        "source_segment_ids": proposal.source_segment_ids,
        "source_segment_revisions": {
            segment_id: source_segment_revisions[segment_id]
            for segment_id in proposal.source_segment_ids
        },
    }
    model_types = {
        "topic": Topic,
        "decision": Decision,
        "highlight": Highlight,
        "conflict": Conflict,
    }
    return model_types[kind](**values)


def _entity(
    proposal: EntityProposal,
    source_segment_revisions: Mapping[str, int],
) -> Entity:
    return Entity(
        item_id=stable_item_id(
            kind="entity",
            content={
                "text": proposal.text,
                "entity_type": proposal.entity_type,
            },
            evidence_ids=proposal.source_segment_ids,
        ),
        text=proposal.text,
        entity_type=proposal.entity_type,
        source_segment_ids=proposal.source_segment_ids,
        source_segment_revisions={
            segment_id: source_segment_revisions[segment_id]
            for segment_id in proposal.source_segment_ids
        },
    )


def _merge_items(
    current: Sequence[MeetingStateItem],
    projected: Sequence[MeetingStateItem],
    *,
    reprojected_segment_ids: frozenset[str],
) -> tuple[MeetingStateItem, ...]:
    retained = {
        item.item_id: item
        for item in current
        if set(item.source_segment_ids).isdisjoint(reprojected_segment_ids)
    }
    retained.update({item.item_id: item for item in projected})
    return tuple(retained[item_id] for item_id in sorted(retained))


def merge_meeting_state(
    current: MeetingState,
    delta: MeetingStateDelta,
    *,
    action_candidates: Sequence[ActionCandidate],
    source_segment_revisions: Mapping[str, int],
    next_version: int,
) -> MeetingState:
    if current.session_id != delta.session_id:
        raise ValueError("Meeting State delta belongs to another Session")
    if next_version <= current.version:
        raise ValueError("Meeting State version must advance")
    replaced = frozenset(delta.source_segment_ids)
    if set(source_segment_revisions) != set(delta.source_segment_ids):
        raise ValueError("projection revisions must match delta Segment IDs")
    if any(value < 1 for value in source_segment_revisions.values()):
        raise ValueError("projection revisions must be positive")

    return MeetingState(
        session_id=current.session_id,
        version=next_version,
        topics=_merge_items(
            current.topics,
            tuple(
                _state_item("topic", value, source_segment_revisions)
                for value in delta.topics
            ),
            reprojected_segment_ids=replaced,
        ),
        entities=_merge_items(
            current.entities,
            tuple(_entity(value, source_segment_revisions) for value in delta.entities),
            reprojected_segment_ids=replaced,
        ),
        decisions=_merge_items(
            current.decisions,
            tuple(
                _state_item("decision", value, source_segment_revisions)
                for value in delta.decisions
            ),
            reprojected_segment_ids=replaced,
        ),
        action_candidates=tuple(
            sorted(action_candidates, key=lambda value: value.candidate_id)
        ),
        highlights=_merge_items(
            current.highlights,
            tuple(
                _state_item("highlight", value, source_segment_revisions)
                for value in delta.highlights
            ),
            reprojected_segment_ids=replaced,
        ),
        conflicts=_merge_items(
            current.conflicts,
            tuple(
                _state_item("conflict", value, source_segment_revisions)
                for value in delta.conflicts
            ),
            reprojected_segment_ids=replaced,
        ),
        # User concerns are command-owned and never removed by caption projection.
        user_concerns=current.user_concerns,
    )


def canonical_state_hash(state: MeetingState) -> str:
    semantic_state = state.model_dump(mode="json", exclude={"version"})
    payload = json.dumps(
        semantic_state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
