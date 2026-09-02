from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.evals.full_chain import run_full_chain_evaluation


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the two-scenario local-media Meeting Agent evaluation"
    )
    parser.add_argument(
        "--audio",
        default=str(BACKEND / "tests" / "fixtures" / "demo_audio.wav"),
    )
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "reports" / "meeting-agent-product-v1"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="meeting-agent-full-chain-") as temp:
        summary, traces = asyncio.run(
            run_full_chain_evaluation(
                audio_path=args.audio,
                output_dir=args.output_dir,
                work_dir=temp,
            )
        )
    print(
        f"overall_result={summary['overall_result']} "
        f"scenarios={summary['passed_scenario_count']}/{summary['scenario_count']}"
    )
    trace_dir = Path(args.output_dir).resolve() / "full-chain-traces"
    for name, trace in traces.items():
        scenario = next(
            value for value in summary["scenarios"] if value["scenario"] == name
        )
        print(
            f"scenario={name} trace_id={trace.trace_id} "
            f"terminal={scenario['terminal_status']} "
            f"trace={trace_dir / f'{name}.json'}"
        )
    return 0 if summary["overall_result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
