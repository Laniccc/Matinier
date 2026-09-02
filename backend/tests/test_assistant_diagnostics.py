from __future__ import annotations

import hashlib
import json
import logging
from types import SimpleNamespace

from app.assistant.repository import AssistantRepository
from app.logging import JsonFormatter, log_assistant_lifecycle
from app.persistence.models import SessionRecord


def test_lifecycle_log_uses_trace_correlation_without_sensitive_content(
    meeting_history,
    caplog,
) -> None:
    fixture = meeting_history
    with fixture.database.session() as db_session:
        repository = AssistantRepository(db_session)
        execution = repository.get_execution_required(fixture.child_id)
        trace = repository.export_trace(
            execution.id,
            media_session_id=fixture.media_id,
        )

    caplog.set_level(logging.INFO, logger="app.assistant.lifecycle")
    log_assistant_lifecycle(
        "assistant_tool_result",
        session_id=execution.session_id,
        execution_id=execution.id,
        root_execution_id=execution.root_execution_id,
        status="failed",
        elapsed_ms=27,
        phase="execute",
        tool_call_id="tool-call-1",
        retry_count=2,
        error_code="tool_execution_failed",
        error_message="Bearer private-token",
        goal="confidential meeting goal",
    )

    record = next(
        value
        for value in reversed(caplog.records)
        if getattr(value, "event", None) == "assistant_tool_result"
    )
    payload = json.loads(JsonFormatter("test").format(record))
    assert payload["trace_id"] == trace.trace_id == execution.root_execution_id
    assert payload["execution_id"] == execution.id
    assert payload["tool_call_id"] == "tool-call-1"
    assert payload["phase"] == "execute"
    assert payload["duration_ms"] == 27
    assert payload["retry_count"] == 2
    assert payload["error_code"] == "tool_execution_failed"
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "private-token" not in encoded
    assert "confidential meeting goal" not in encoded


def test_readiness_diagnostics_are_read_only_and_count_recovery_backlog(
    client,
) -> None:
    database = client.app.state.database
    with database.session() as db_session:
        db_session.add(
            SessionRecord(
                id="diagnostic-session",
                room_name="diagnostic-room",
                status="active",
                source_type="browser_tab",
                source_name="fixture",
                language="en",
            )
        )
        db_session.flush()
        repository = AssistantRepository(db_session)
        execution = repository.create_execution(
            session_id="diagnostic-session",
            profile="action_run",
            goal="diagnostic fixture",
            execution_id="diagnostic-execution",
        )
        call = repository.prepare_tool_call(
            execution_id=execution.id,
            tool_name="fixture.write",
            tool_version="1",
            capability="fixture.write",
            effect="external_write",
            arguments={},
            arguments_hash=hashlib.sha256(b"{}").hexdigest(),
            logical_action_key="diagnostic-action",
            idempotency_key="diagnostic-idempotency",
            tool_call_id="diagnostic-tool-call",
        )
        call = repository.update_tool_call(
            call.id,
            expected_status="prepared",
            status="requesting",
            increment_attempt=True,
        )
        repository.update_tool_call(
            call.id,
            expected_status="requesting",
            status="unknown",
            error_code="fixture_unknown",
        )
        db_session.commit()

    response = client.get("/health/ready")
    assert response.status_code == 200
    assistant = response.json()["assistant"]
    assert assistant["tool_call_unknown_count"] == 1
    assert assistant["tool_call_reconciling_count"] == 0
    assert assistant["recovery_backlog_count"] == 1

    with database.session() as db_session:
        repository = AssistantRepository(db_session)
        assert repository.get_execution_required(execution.id).status == "queued"
        assert repository.get_tool_call(call.id).status == "unknown"


def test_readiness_exposes_live_runner_counts_without_invoking_runners(client) -> None:
    scheduler = SimpleNamespace(
        started=True,
        queue_depth=3,
        active_count=2,
        oldest_queued_age_seconds=4.5,
    )
    client.app.state.assistant_runtime = SimpleNamespace(
        projector=SimpleNamespace(started=True),
        fast_runner=SimpleNamespace(queue_depth=0, active_count=1),
        action_runtime=SimpleNamespace(scheduler=scheduler),
        task_adapter=SimpleNamespace(provider_name="disabled"),
    )

    assistant = client.get("/health/ready").json()["assistant"]
    assert assistant["runtime_started"] is True
    assert assistant["fast_turn_active_count"] == 1
    assert assistant["action_run_queue_depth"] == 3
    assert assistant["action_run_active_count"] == 2
    assert assistant["action_run_oldest_queued_age_seconds"] == 4.5
