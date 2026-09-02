from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from app.evals.contracts import EvalSuite
from app.evals.graders import TrialGrade
from app.evals.metrics import EvaluationSummary
from app.evals.scenario_runner import TrialResult


def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_report(output_dir: str | Path, *, suite: EvalSuite, trials_per_case: int, seed: int,
                 results: list[TrialResult], grades: list[TrialGrade], summary: EvaluationSummary) -> Path:
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    traces_dir = output / "traces"
    failures_dir = output / "failures"
    traces_dir.mkdir(exist_ok=True)
    failures_dir.mkdir(exist_ok=True)
    for stale in failures_dir.glob("*.json"):
        stale.unlink()
    _write_json(output / "manifest.json", {
        "suite_id": suite.suite_id, "suite_hash": suite.suite_hash,
        "code_identifier": "workspace", "provider": "scripted", "seed": seed,
        "trials_per_case": trials_per_case, "trial_count": len(results),
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
    })
    _write_json(output / "summary.json", summary.model_dump(mode="json"))
    _write_json(output / "metrics.json", {
        "trials": [{
            "case_id": result.case_id, "trial": result.trial,
            "duration_ms": result.duration_ms, "model_calls": result.model_calls,
            "planning_rounds": result.planning_rounds, "tool_calls": result.tool_calls,
            "tool_attempts": result.tool_attempts,
            "confirmed_side_effects": result.confirmed_side_effects,
            "passed": grade.passed, "violations": list(grade.violations),
        } for result, grade in zip(results, grades)],
        "aggregate": [metric.model_dump(mode="json") for metric in summary.metrics],
    })
    index_lines = []
    for result, grade in zip(results, grades):
        path = traces_dir / result.case_id / f"trial-{result.trial}.json"
        _write_json(path, result.trace.model_dump(mode="json"))
        index_lines.append(json.dumps({
            "case_id": result.case_id, "trial": result.trial,
            "trace_id": result.trace.trace_id, "root_execution_id": result.trace.root_execution_id,
            "terminal_status": result.terminal_status, "result": "PASS" if grade.passed else "FAIL",
            "path": path.relative_to(output).as_posix(),
        }, ensure_ascii=False, sort_keys=True))
        if not grade.passed:
            _write_json(failures_dir / f"{result.case_id}-trial-{result.trial}.json", {
                "case_id": result.case_id, "trial": result.trial,
                "violations": list(grade.violations),
                "node_ids": [error.node_id for error in __import__(
                    "app.evals.trace_validator", fromlist=["validate_trace"]).validate_trace(result.trace).errors],
            })
    (output / "trace-index.jsonl").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    lines = ["# Meeting Agent Product Eval", "", f"- Overall: **{summary.overall_result}**",
        f"- Scope: `scripted_local`", f"- Trials: {summary.passed_trial_count}/{summary.trial_count}", "",
        "| Metric | Actual | Target | Result | Samples | Scope |", "|---|---:|---:|---|---:|---|"]
    lines += [f"| `{m.metric}` | {m.actual} | {m.target} | {m.result} | {m.sample_count} | {m.scope} |"
        for m in summary.metrics]
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output


__all__ = ["write_report"]
