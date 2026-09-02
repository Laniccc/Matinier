from pathlib import Path

from app.evals.contracts import load_eval_suite
from app.evals.graders import grade_trial
from app.evals.metrics import summarize
from app.evals.scenario_runner import ScenarioRunner


CASES = Path(__file__).parents[1] / "evals" / "cases" / "meeting_agent_product_v1.jsonl"


def by_name(summary):
    return {metric.metric: metric for metric in summary.metrics}


def test_metrics_keep_na_denominators_percentiles_and_safe_gate_priority():
    suite = load_eval_suite(CASES)
    fast_cases = list(suite.cases[:3])
    results = [ScenarioRunner().run(case, trial=1, seed=20260901) for case in fast_cases]
    results = [result.model_copy(update={"duration_ms": value})
        for result, value in zip(results, (10.0, 20.0, 100.0))]
    grades = [grade_trial(case, result) for case, result in zip(fast_cases, results)]
    summary = summarize(grades, results)
    metrics = by_name(summary)
    assert metrics["trace.external_lineage_complete_rate"].result == "N/A"
    assert metrics["efficiency.wall_time_p50_ms"].actual == 20.0
    assert metrics["efficiency.wall_time_p95_ms"].actual == 100.0
    assert metrics["cost.input_tokens"].actual == "N/A"

    unsafe = grades[0].model_copy(update={"passed": False,
        "violations": ("unauthorized_write",), "unauthorized_writes": 1})
    quality = grades[1].model_copy(update={"passed": False,
        "violations": ("route_mismatch",), "route_correct": False})
    failed = summarize([unsafe, quality, grades[2]], results)
    assert failed.overall_result == "FAIL_SAFE"
