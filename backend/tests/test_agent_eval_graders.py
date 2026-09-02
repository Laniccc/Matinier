from pathlib import Path

from app.evals.contracts import load_eval_suite
from app.evals.graders import grade_trial
from app.evals.scenario_runner import ScenarioRunner


CASES = Path(__file__).parents[1] / "evals" / "cases" / "meeting_agent_product_v1.jsonl"


def test_grader_reads_structured_trace_and_rejects_missing_grant_lineage():
    case = next(case for case in load_eval_suite(CASES).cases if case.case_id == "action_execute")
    result = ScenarioRunner().run(case, trial=1, seed=20260901)
    assert grade_trial(case, result).passed
    broken = result.trace.model_copy(update={
        "edges": tuple(edge for edge in result.trace.edges
            if not (edge.kind == "authorized_by" and edge.source.startswith("tool:")))
    })
    grade = grade_trial(case, result.model_copy(update={"trace": broken}))
    assert not grade.passed
    assert "grant_edge_missing" in grade.violations
    assert grade.unauthorized_writes == 1


def test_grader_detects_sensitive_structured_field_without_parsing_logs():
    case = load_eval_suite(CASES).cases[0]
    result = ScenarioRunner().run(case, trial=1, seed=20260901)
    target = result.trace.nodes[0]
    altered = target.model_copy(update={"data": {**target.data, "prompt": "sensitive"}})
    trace = result.trace.model_copy(update={
        "nodes": (altered, *result.trace.nodes[1:])
    })
    grade = grade_trial(case, result.model_copy(update={"trace": trace}))
    assert grade.sensitive_value_hits == 1
    assert not grade.passed
