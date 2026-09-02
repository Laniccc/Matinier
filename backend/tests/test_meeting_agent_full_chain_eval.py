from __future__ import annotations

import asyncio
from pathlib import Path

from app.evals.full_chain import run_full_chain_evaluation
from app.evals.trace_validator import validate_trace


def test_local_media_full_chain_covers_normal_and_recovery_paths(tmp_path) -> None:
    audio = Path(__file__).parent / "fixtures" / "demo_audio.wav"
    output = tmp_path / "report"
    summary, traces = asyncio.run(
        run_full_chain_evaluation(
            audio_path=audio,
            output_dir=output,
            work_dir=tmp_path / "work",
        )
    )

    assert summary["scope"] == "local_media_diagnostic"
    assert summary["overall_result"] == "PASS"
    assert summary["passed_scenario_count"] == summary["scenario_count"] == 2
    assert set(traces) == {"normal", "response_lost"}
    assert all(validate_trace(trace).valid for trace in traces.values())
    lost = next(
        scenario
        for scenario in summary["scenarios"]
        if scenario["scenario"] == "response_lost"
    )
    assert lost["create_call_count"] == 1
    assert lost["reconcile_call_count"] >= 1
    assert lost["unknown_direct_retry_count"] == 0
    assert lost["evidence_to_agent_trace_join_rate_percent"] == 100
    assert (output / "full-chain-summary.json").is_file()
    assert (output / "full-chain-summary.md").is_file()
    assert len(list((output / "full-chain-traces").glob("*.json"))) == 2


def test_full_chain_report_has_actual_target_result_and_scope(tmp_path) -> None:
    summary, _ = asyncio.run(
        run_full_chain_evaluation(
            audio_path=Path(__file__).parent / "fixtures" / "demo_audio.wav",
            output_dir=tmp_path / "report",
            work_dir=tmp_path / "work",
        )
    )
    by_name = {metric["metric"]: metric for metric in summary["metrics"]}
    required = {
        "input.decoded_frame_count",
        "caption.final_to_evidence_rate_percent",
        "connection.evidence_to_agent_trace_join_rate_percent",
        "agent.trace_integrity_percent",
        "safety.unauthorized_write_count",
        "safety.duplicate_task_count",
        "safety.unknown_direct_retry_count",
        "recovery.response_lost_create_call_count",
        "latency.reconcile_duration_ms",
    }
    assert required <= set(by_name)
    for metric in by_name.values():
        assert metric["actual"] is not None
        assert metric["target"] is not None
        assert metric["result"] == "PASS"
        assert metric["scope"] == "local_media_diagnostic"
        assert metric["sample_count"] >= 1
