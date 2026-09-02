# Meeting Agent Full-chain Local Media Evaluation

- Overall: **PASS**
- Scope: `local_media_diagnostic`
- Scenarios: 2/2
- Token/cost: N/A (scripted providers)

| Metric | Actual | Target | Result | Samples | Scope |
|---|---:|---:|---|---:|---|
| `input.decoded_frame_count` | 50 | >0 | PASS | 2 | local_media_diagnostic |
| `input.audio_duration_seconds` | 1.0 | >0 | PASS | 2 | local_media_diagnostic |
| `input.dropped_frames` | 0 | 0 | PASS | 2 | local_media_diagnostic |
| `caption.final_count` | 1 | >=1 | PASS | 2 | local_media_diagnostic |
| `caption.provider_error_count` | 0 | 0 | PASS | 2 | local_media_diagnostic |
| `caption.final_to_evidence_rate_percent` | 100.0 | 100 | PASS | 2 | local_media_diagnostic |
| `connection.evidence_to_agent_trace_join_rate_percent` | 100.0 | 100 | PASS | 2 | local_media_diagnostic |
| `agent.route_terminal_accuracy_percent` | 100.0 | 100 | PASS | 2 | local_media_diagnostic |
| `agent.trace_integrity_percent` | 100.0 | 100 | PASS | 2 | local_media_diagnostic |
| `safety.unauthorized_write_count` | 0 | 0 | PASS | 2 | local_media_diagnostic |
| `safety.duplicate_task_count` | 0 | 0 | PASS | 2 | local_media_diagnostic |
| `safety.unknown_direct_retry_count` | 0 | 0 | PASS | 2 | local_media_diagnostic |
| `recovery.response_lost_create_call_count` | 1 | 1 | PASS | 1 | local_media_diagnostic |
| `recovery.response_lost_reconcile_call_count` | 2 | >=1 | PASS | 1 | local_media_diagnostic |
| `latency.audio_to_first_partial_ms` | 55.287 | diagnostic_only | PASS | 2 | local_media_diagnostic |
| `latency.audio_to_final_ms` | 62.553 | diagnostic_only | PASS | 2 | local_media_diagnostic |
| `latency.final_to_snapshot_ms` | 59.031 | diagnostic_only | PASS | 2 | local_media_diagnostic |
| `latency.snapshot_to_fast_terminal_or_handoff_ms` | 13.156 | diagnostic_only | PASS | 2 | local_media_diagnostic |
| `latency.handoff_to_action_terminal_ms` | 149.226 | diagnostic_only | PASS | 2 | local_media_diagnostic |
| `latency.reconcile_duration_ms` | 59.623 | diagnostic_only | PASS | 1 | local_media_diagnostic |

## Trace files

- `normal`: `full-chain-traces/normal.json` (`f41afea0-8405-410e-93a3-1ffe6c67545a`)
- `response_lost`: `full-chain-traces/response_lost.json` (`a6b91e0d-ad0f-4be0-895e-8252e1faf1ec`)
