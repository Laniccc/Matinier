from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.evals.demo import run_offline_demo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run five offline Meeting Agent demos")
    parser.add_argument(
        "--suite",
        default=str(BACKEND / "evals" / "cases" / "meeting_agent_product_v1.jsonl"),
    )
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "reports" / "meeting-agent-demo"),
    )
    args = parser.parse_args(argv)
    summary, results = run_offline_demo(
        suite_path=args.suite,
        output_dir=args.output_dir,
    )
    print(
        f"overall_result={summary.overall_result} "
        f"demos={summary.passed_trial_count}/{summary.trial_count}"
    )
    for result in results:
        trace_path = (
            Path(args.output_dir).resolve()
            / "traces"
            / result.case_id
            / f"trial-{result.trial}.json"
        )
        print(
            f"scenario={result.case_id} trace_id={result.trace.trace_id} "
            f"terminal={result.terminal_status} trace={trace_path}"
        )
    return 0 if summary.overall_result == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
