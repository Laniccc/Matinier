import json
from pathlib import Path

import pytest

from app.evals.contracts import EvalCase, load_eval_suite


CASES = Path(__file__).parents[1] / "evals" / "cases" / "meeting_agent_product_v1.jsonl"


def test_product_suite_has_exactly_twelve_unique_traceable_cases():
    suite = load_eval_suite(CASES)
    assert len(suite.cases) == 12
    assert len({case.case_id for case in suite.cases}) == 12
    assert all(set(case.expected_evidence_refs) <= set(case.input_evidence) for case in suite.cases)
    assert all(case.max_side_effects is not None for case in suite.cases
        if case.expected_tool_effect == "external_write")


def test_eval_contract_rejects_unknown_evidence_fault_and_write_without_budget(tmp_path):
    base = {
        "case_id": "bad_case", "description": "bad", "route": "action",
        "terminal_statuses": ["completed"], "input_evidence": ["e1"],
        "expected_evidence_refs": ["missing"], "max_tool_calls": 0,
    }
    with pytest.raises(ValueError):
        EvalCase.model_validate(base)
    with pytest.raises(ValueError):
        EvalCase.model_validate({**base, "expected_evidence_refs": ["e1"], "fault": "disk_fire"})
    with pytest.raises(ValueError):
        EvalCase.model_validate({**base, "expected_evidence_refs": ["e1"],
            "expected_tool_name": "task.create", "expected_tool_effect": "external_write",
            "max_tool_calls": 1})

    duplicate = {**base, "expected_evidence_refs": ["e1"], "route": "fast"}
    path = tmp_path / "duplicate.jsonl"
    line = json.dumps(duplicate)
    path.write_text(line + "\n" + line + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_eval_suite(path)
