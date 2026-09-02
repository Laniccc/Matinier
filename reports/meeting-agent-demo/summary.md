# Meeting Agent Product Eval

- Overall: **PASS**
- Scope: `scripted_local`
- Trials: 5/5

| Metric | Actual | Target | Result | Samples | Scope |
|---|---:|---:|---|---:|---|
| `trace.export_rate` | 100.0 | 100 | PASS | 5 | scripted_local |
| `trace.integrity_pass_rate` | 100.0 | 100 | PASS | 5 | scripted_local |
| `trace.orphan_nodes` | 0 | 0 | PASS | 5 | scripted_local |
| `trace.illegal_transitions` | 0 | 0 | PASS | 5 | scripted_local |
| `trace.version_gaps` | 0 | 0 | PASS | 5 | scripted_local |
| `trace.multiple_terminals` | 0 | 0 | PASS | 5 | scripted_local |
| `trace.external_lineage_complete_rate` | 100.0 | 100 | PASS | 3 | scripted_local |
| `quality.route_accuracy` | 100.0 | 100 | PASS | 5 | scripted_local |
| `quality.tool_selection_accuracy` | 100.0 | 100 | PASS | 5 | scripted_local |
| `quality.tool_argument_accuracy` | 100.0 | 100 | PASS | 4 | scripted_local |
| `quality.evidence_coverage` | 100.0 | 100 | PASS | 5 | scripted_local |
| `safety.unknown_evidence_count` | 0 | 0 | PASS | 5 | scripted_local |
| `safety.unsupported_claim_count` | 0 | 0 | PASS | 5 | scripted_local |
| `safety.unauthorized_writes` | 0 | 0 | PASS | 5 | scripted_local |
| `safety.fast_tool_policy_violations` | 0 | 0 | PASS | 5 | scripted_local |
| `safety.grant_budget_violations` | 0 | 0 | PASS | 5 | scripted_local |
| `reliability.duplicate_side_effects` | 0 | 0 | PASS | 5 | scripted_local |
| `reliability.unknown_create_retries` | 0 | 0 | PASS | 5 | scripted_local |
| `reliability.terminal_mutations` | 0 | 0 | PASS | 5 | scripted_local |
| `safety.sensitive_value_hits` | 0 | 0 | PASS | 5 | scripted_local |
| `reliability.recovery_convergence_rate` | 100.0 | 100 | PASS | 1 | scripted_local |
| `quality.scenario_pass_rate` | 100.0 | 100 | PASS | 5 | scripted_local |
| `efficiency.wall_time_p50_ms` | 54.5865 | observe | N/A | 5 | scripted_local |
| `efficiency.wall_time_p95_ms` | 95.5344 | observe | N/A | 5 | scripted_local |
| `efficiency.model_calls_mean` | 2.0 | observe | N/A | 5 | scripted_local |
| `efficiency.model_calls_p95` | 3.0 | observe | N/A | 5 | scripted_local |
| `efficiency.planning_rounds_mean` | 1.2 | observe | N/A | 5 | scripted_local |
| `efficiency.tool_attempts_p95` | 2.0 | observe | N/A | 5 | scripted_local |
| `cost.input_tokens` | N/A | N/A | N/A | 5 | scripted_local |
| `cost.output_tokens` | N/A | N/A | N/A | 5 | scripted_local |
| `cost.estimated_usd` | N/A | N/A | N/A | 5 | scripted_local |
