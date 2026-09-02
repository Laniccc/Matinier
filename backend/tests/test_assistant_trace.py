import json
import datetime as dt

from app.assistant.context import ContextBuilder
from app.assistant.repository import AssistantRepository
from app.evals.trace_validator import validate_trace


def test_repository_exports_stable_redacted_trace_from_persisted_execution(meeting_history):
    f = meeting_history
    secret_goal = "Explain the confidential release sentence"
    with f.database.session() as db:
        repo = AssistantRepository(db)
        snapshot = ContextBuilder(db).build_sync(
            session_id=f.session_id,
            goal=secret_goal,
            actor_id="local-user",
            persist=True,
        )
        execution = repo.create_execution(
            session_id=f.session_id,
            profile="fast_turn",
            goal=secret_goal,
            snapshot_id=snapshot.snapshot_id,
            client_request_id="trace-fast",
        )
        for status in ("contextualizing", "deciding"):
            execution = repo.transition_execution(execution.id,
                expected_version=execution.state_version, target_status=status,
                event_type=f"trace.{status}", summary=status)
        repo.append_step(execution_id=execution.id, kind="plan",
            input_payload={"round": 1}, output_payload={"decision": "respond"})
        for status in ("responding", "completed"):
            execution = repo.transition_execution(execution.id,
                expected_version=execution.state_version, target_status=status,
                event_type=f"trace.{status}", summary=status,
                result={"claims": [{"evidence_refs": list(snapshot.evidence_refs)}]} if status == "completed" else None)
        db.commit()
        trace = repo.export_trace(execution.id, media_session_id=f.media_id)

    assert trace.trace_id == execution.id == trace.root_execution_id
    def key(node):
        timestamp = node.timestamp if node.timestamp.tzinfo else node.timestamp.replace(tzinfo=dt.UTC)
        return timestamp.astimezone(dt.UTC), node.state_version or 0, node.node_id
    assert tuple(trace.nodes) == tuple(sorted(trace.nodes, key=key))
    assert validate_trace(trace).valid
    encoded = json.dumps(trace.model_dump(mode="json"), ensure_ascii=False)
    assert secret_goal not in encoded
    assert "Release notes" not in encoded
    assert any(node.kind == "input_anchor" and node.data.get("revision") == 2 for node in trace.nodes)
    assert {"context", "execution", "model_stage", "terminal", "input_anchor"} <= {node.kind for node in trace.nodes}
