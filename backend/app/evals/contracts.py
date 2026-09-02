from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EvalRoute = Literal["fast", "handoff", "action", "needs_input"]
FaultKind = Literal[
    "none", "prompt_injection", "http_429", "http_5xx", "response_lost",
    "duplicate_request", "concurrent_same_action", "user_rejected",
]


class FrozenEvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvalCase(FrozenEvalModel):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    description: str = Field(min_length=1, max_length=500)
    route: EvalRoute
    terminal_statuses: tuple[str, ...] = Field(min_length=1)
    input_evidence: tuple[str, ...] = Field(min_length=1)
    expected_evidence_refs: tuple[str, ...] = Field(min_length=1)
    expected_tool_name: str | None = Field(default=None, max_length=128)
    expected_tool_effect: Literal["read", "local_write", "external_write"] | None = None
    expected_arguments_subset: dict[str, object] = Field(default_factory=dict)
    max_tool_calls: int = Field(default=0, ge=0, le=20)
    max_side_effects: int | None = Field(default=None, ge=0, le=20)
    fault: FaultKind = "none"
    required_node_kinds: tuple[str, ...] = Field(default_factory=tuple)
    required_edge_kinds: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def validate_lineage(self):
        if not set(self.expected_evidence_refs) <= set(self.input_evidence):
            raise ValueError("expected evidence must be declared in input_evidence")
        if self.expected_tool_name is None:
            if self.expected_tool_effect is not None or self.expected_arguments_subset:
                raise ValueError("tool expectations require expected_tool_name")
        elif self.max_tool_calls < 1:
            raise ValueError("tool cases require a positive max_tool_calls")
        if self.expected_tool_effect == "external_write" and self.max_side_effects is None:
            raise ValueError("external-write cases require max_side_effects")
        if self.max_side_effects is not None and self.expected_tool_effect != "external_write":
            raise ValueError("side-effect limits apply only to external-write cases")
        return self


class EvalSuite(FrozenEvalModel):
    suite_id: str
    cases: tuple[EvalCase, ...]
    suite_hash: str


def load_eval_suite(path: str | Path) -> EvalSuite:
    source = Path(path)
    rows = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(EvalCase.model_validate_json(line))
        except Exception as error:
            raise ValueError(f"invalid eval case at line {line_number}: {error}") from error
    identifiers = [row.case_id for row in rows]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("eval suite contains duplicate case IDs")
    if not rows:
        raise ValueError("eval suite is empty")
    canonical = "\n".join(json.dumps(row.model_dump(mode="json"), ensure_ascii=False,
        sort_keys=True, separators=(",", ":")) for row in rows)
    return EvalSuite(
        suite_id=source.stem,
        cases=tuple(rows),
        suite_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


__all__ = ["EvalCase", "EvalRoute", "EvalSuite", "FaultKind", "load_eval_suite"]
