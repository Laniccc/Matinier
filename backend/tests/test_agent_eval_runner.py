from pathlib import Path

from app.evals.contracts import load_eval_suite
from app.evals.graders import grade_trial
from app.evals.scenario_runner import ScenarioRunner


CASES = Path(__file__).parents[1] / "evals" / "cases" / "meeting_agent_product_v1.jsonl"


def test_scripted_runner_produces_twelve_valid_durable_traces():
    suite = load_eval_suite(CASES)
    runner = ScenarioRunner()
    results = [runner.run(case, trial=1, seed=20260901) for case in suite.cases]
    grades = [grade_trial(case, result) for case, result in zip(suite.cases, results)]
    assert len(results) == 12
    assert all(grade.passed for grade in grades)
    assert all(result.trace.trace_id == result.trace.root_execution_id for result in results)
    assert next(result for result in results if result.case_id == "unknown_reconcile").tool_attempts == 2
    assert next(result for result in results if result.case_id == "unknown_reconcile").confirmed_side_effects == 1
