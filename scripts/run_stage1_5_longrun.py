from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.diagnostics.longrun import (  # noqa: E402
    FailureInjection,
    LongRunConfig,
    LongRunConfigurationError,
    LongRunMode,
    LongRunSourceType,
    execute_longrun,
    write_longrun_report,
)
from app.settings import Settings  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a bounded Stage 1.5 transport/provider diagnostic and write "
            "JSON plus Markdown evidence."
        )
    )
    parser.add_argument("mode", choices=[item.value for item in LongRunMode])
    parser.add_argument(
        "--duration-minutes",
        type=float,
        required=True,
        help="Required finite duration; real provider smoke is limited to 3-10.",
    )
    parser.add_argument(
        "--source-type",
        choices=[item.value for item in LongRunSourceType],
        required=True,
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Local WAV/MP3 path or public M3U8 URL.",
    )
    parser.add_argument("--reports-dir", type=Path)
    parser.add_argument(
        "--allow-cloud",
        action="store_true",
        help="Required acknowledgement for real-provider-smoke.",
    )
    parser.add_argument("--with-translation", action="store_true")
    parser.add_argument("--source-language", default="zh-CN")
    parser.add_argument("--target-language", default="en-US")
    parser.add_argument(
        "--inject",
        action="append",
        default=[],
        choices=[item.value for item in FailureInjection],
        help="Bounded deterministic injection; repeat for multiple cases.",
    )
    parser.add_argument("--queue-max-frames", type=int, default=100)
    parser.add_argument("--consumer-delay-ms", type=float, default=0.0)
    parser.add_argument(
        "--fake-chunks-per-segment",
        type=int,
        default=60,
        help=(
            "Fake-provider audio chunks per caption segment (3-600); "
            "60 means about six seconds at the default 100 ms chunk size."
        ),
    )
    return parser


def build_config(
    args: argparse.Namespace,
    *,
    settings: Settings,
) -> LongRunConfig:
    return LongRunConfig(
        mode=LongRunMode(args.mode),
        source_type=LongRunSourceType(args.source_type),
        source=args.source,
        duration_minutes=args.duration_minutes,
        reports_dir=args.reports_dir or settings.reports_dir,
        allow_cloud=args.allow_cloud,
        with_translation=args.with_translation,
        source_language=args.source_language,
        target_language=args.target_language,
        failure_injections=frozenset(
            FailureInjection(item) for item in args.inject
        ),
        queue_max_frames=args.queue_max_frames,
        consumer_delay_ms=args.consumer_delay_ms,
        fake_chunks_per_segment=args.fake_chunks_per_segment,
    )


async def _run(config: LongRunConfig, settings: Settings) -> int:
    report = await execute_longrun(config, settings=settings)
    artifacts = write_longrun_report(report, config.reports_dir)
    print(f"JSON report: {artifacts.json_path}")
    print(f"Markdown report: {artifacts.markdown_path}")
    print(f"Exit reason: {report.exit_reason}")
    expected_failure = bool(config.failure_injections)
    return 0 if report.exit_reason == "completed" or expected_failure else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = Settings()
    try:
        config = build_config(args, settings=settings)
    except LongRunConfigurationError as error:
        parser.error(str(error))
    return asyncio.run(_run(config, settings))


if __name__ == "__main__":
    raise SystemExit(main())
