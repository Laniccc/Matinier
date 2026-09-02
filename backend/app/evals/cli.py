from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

from app.assistant.trace import AgentTrace
from app.evals.contracts import EvalSuite, load_eval_suite
from app.evals.graders import TrialGrade, grade_trial
from app.evals.metrics import EvaluationSummary, summarize
from app.evals.report import write_report
from app.evals.scenario_runner import ScenarioRunner, TrialResult


def evaluate_suite(suite: EvalSuite, *, trials_per_case: int, seed: int,
                   trace_mutator: Callable[[str, int, AgentTrace], AgentTrace] | None = None):
    runner = ScenarioRunner()
    results: list[TrialResult] = []
    grades: list[TrialGrade] = []
    for case in suite.cases:
        for trial_number in range(1, trials_per_case + 1):
            result = runner.run(case, trial=trial_number, seed=seed)
            if trace_mutator is not None:
                result = result.model_copy(update={
                    "trace": trace_mutator(case.case_id, trial_number, result.trace)
                })
            results.append(result)
            grades.append(grade_trial(case, result))
    return results, grades, summarize(grades, results)


def build_parser():
    parser = argparse.ArgumentParser(description="Run the scripted Meeting Agent product eval")
    parser.add_argument("--suite", required=True)
    parser.add_argument("--provider", choices=("scripted",), default="scripted")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260901)
    parser.add_argument("--require-complete-trace", action="store_true")
    parser.add_argument("--output-dir", required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.trials < 1:
        raise SystemExit("--trials must be positive")
    suite = load_eval_suite(args.suite)
    results, grades, summary = evaluate_suite(suite, trials_per_case=args.trials, seed=args.seed)
    write_report(args.output_dir, suite=suite, trials_per_case=args.trials, seed=args.seed,
        results=results, grades=grades, summary=summary)
    print(f"overall_result={summary.overall_result} trials={summary.passed_trial_count}/{summary.trial_count}")
    print(f"report={Path(args.output_dir).resolve()}")
    return 0 if summary.overall_result == "PASS" else 2 if summary.overall_result == "FAIL_SAFE" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["evaluate_suite", "main"]
