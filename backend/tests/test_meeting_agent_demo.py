from __future__ import annotations

from pathlib import Path

from app.evals.demo import DEMO_CASES, run_offline_demo
from app.evals.trace_validator import validate_trace


def test_offline_demo_emits_one_valid_trace_per_product_story(tmp_path) -> None:
    suite = Path(__file__).parents[1] / "evals" / "cases" / "meeting_agent_product_v1.jsonl"
    output = tmp_path / "demo"
    summary, results = run_offline_demo(
        suite_path=suite,
        output_dir=output,
    )

    assert summary.overall_result == "PASS"
    assert summary.passed_trial_count == summary.trial_count == len(DEMO_CASES)
    assert tuple(result.case_id for result in results) == DEMO_CASES
    assert all(validate_trace(result.trace).valid for result in results)
    assert len(list((output / "traces").glob("*/*.json"))) == len(DEMO_CASES)
