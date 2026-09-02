from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.evals.cli import evaluate_suite
from app.evals.contracts import EvalSuite, load_eval_suite
from app.evals.report import write_report


DEMO_CASES = (
    "fast_answer",
    "handoff_slow_read",
    "action_execute",
    "unresolved_assignee",
    "unknown_reconcile",
)


def run_offline_demo(
    *,
    suite_path: str | Path,
    output_dir: str | Path,
    seed: int = 20260901,
) -> tuple[object, list[object]]:
    suite = load_eval_suite(suite_path)
    by_id = {case.case_id: case for case in suite.cases}
    missing = tuple(case_id for case_id in DEMO_CASES if case_id not in by_id)
    if missing:
        raise ValueError(f"Demo suite is missing required cases: {', '.join(missing)}")
    selected = tuple(by_id[case_id] for case_id in DEMO_CASES)
    canonical = json.dumps(
        [case.model_dump(mode="json") for case in selected],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    demo_suite = EvalSuite(
        suite_id="meeting_agent_offline_demo",
        cases=selected,
        suite_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )
    results, grades, summary = evaluate_suite(
        demo_suite,
        trials_per_case=1,
        seed=seed,
    )
    write_report(
        output_dir,
        suite=demo_suite,
        trials_per_case=1,
        seed=seed,
        results=results,
        grades=grades,
        summary=summary,
    )
    return summary, results


__all__ = ["DEMO_CASES", "run_offline_demo"]
