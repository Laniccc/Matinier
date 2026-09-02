import json
from pathlib import Path

import pytest

import app.evals.cli as cli
from app.assistant.trace import TraceEdge
from app.evals.graders import grade_trial
from app.evals.metrics import summarize


CASES = Path(__file__).parents[1] / "evals" / "cases" / "meeting_agent_product_v1.jsonl"


def args(output):
    return ["--suite", str(CASES), "--provider", "scripted", "--trials", "1",
        "--seed", "20260901", "--require-complete-trace", "--output-dir", str(output)]


def test_cli_writes_twelve_traces_and_matching_markdown_json(tmp_path):
    output = tmp_path / "pass"
    assert cli.main(args(output)) == 0
    summary = json.loads((output / "summary.json").read_text("utf-8"))
    markdown = (output / "summary.md").read_text("utf-8")
    assert summary["overall_result"] == "PASS"
    assert len(list((output / "traces").glob("*/trial-1.json"))) == 12
    scenario = next(metric for metric in summary["metrics"] if metric["metric"] == "quality.scenario_pass_rate")
    assert f"| `{scenario['metric']}` | {scenario['actual']} | {scenario['target']} | {scenario['result']} |" in markdown


@pytest.mark.parametrize("mutation,expected", [("grant", 2), ("duplicate", 2), ("quality", 1)])
def test_cli_exit_codes_preserve_safe_before_quality_gate(monkeypatch, tmp_path, mutation, expected):
    original = cli.evaluate_suite

    def wrapped(suite, *, trials_per_case, seed):
        if mutation == "grant":
            def mutate(case_id, trial, trace):
                if case_id != "action_execute":
                    return trace
                return trace.model_copy(update={"edges": tuple(edge for edge in trace.edges
                    if not (edge.kind == "authorized_by" and edge.source.startswith("tool:")))})
            return original(suite, trials_per_case=trials_per_case, seed=seed, trace_mutator=mutate)
        results, grades, _ = original(suite, trials_per_case=trials_per_case, seed=seed)
        if mutation == "quality":
            results[0] = results[0].model_copy(update={"route": "action"})
            grades[0] = grade_trial(suite.cases[0], results[0])
        else:
            index = next(i for i, result in enumerate(results) if result.case_id == "action_execute")
            trace = results[index].trace
            tool = next(node for node in trace.nodes if node.kind == "tool")
            duplicate = tool.model_copy(update={"node_id": tool.node_id + ":duplicate"})
            execution_id = tool.data["execution_id"]
            grant = next(edge.target for edge in trace.edges if edge.source == tool.node_id and edge.kind == "authorized_by")
            claim = next(edge.target for edge in trace.edges if edge.source == tool.node_id and edge.kind == "claimed_by")
            edges = (*trace.edges,
                TraceEdge(source=f"execution:{execution_id}", target=duplicate.node_id, kind="invoked"),
                TraceEdge(source=duplicate.node_id, target=f"execution:{execution_id}", kind="derived_from"),
                TraceEdge(source=duplicate.node_id, target=grant, kind="authorized_by"),
                TraceEdge(source=duplicate.node_id, target=claim, kind="claimed_by"))
            results[index] = results[index].model_copy(update={
                "trace": trace.model_copy(update={"nodes": (*trace.nodes, duplicate), "edges": edges})
            })
            grades[index] = grade_trial(suite.cases[index], results[index])
        return results, grades, summarize(grades, results)

    monkeypatch.setattr(cli, "evaluate_suite", wrapped)
    assert cli.main(args(tmp_path / mutation)) == expected
