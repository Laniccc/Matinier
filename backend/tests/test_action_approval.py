from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.assistant import router as assistant_router
from app.assistant.action_runner import ActionRunRunner, FreshnessCheck, RelevantContextGuard
from app.assistant.context import ContextBuilder
from app.assistant.critic import EvidenceBoundCritic
from app.assistant.grants import (
    GrantAuthorizationError,
    action_grant_from_record,
    authorize_tool_use,
)
from app.assistant.repository import AssistantRepository
from app.assistant.subagents import SubagentCoordinator
from app.assistant.tools import ToolExecutor, ToolRegistry
from app.meeting_state.candidates import caption_evidence_message
from app.meeting_state.contracts import ProjectionSegment
from app.meeting_state.models import ActionCandidateContent, GroundedValue
from app.meeting_state.repository import MeetingStateRepository
from app.persistence.database import Database
from app.persistence.models import (
    ActionGrantRecord,
    AssistantActionApprovalRecord,
    AssistantEventRecord,
    AssistantToolCallRecord,
    utc_now,
)
from app.settings import Settings
from app.task_system.fake import FakeTaskSystemAdapter
from app.task_system.service import TaskSystemService
from app.task_system.tools import TaskSystemInvocationPolicy, register_task_tools
from test_private_meeting_agent_core import (
    _SuccessfulSubagent,
    _TaskLifecyclePlanner,
    _segment,
    _session,
)


def _candidate_content(session_id: str, segment_id: str, text: str) -> tuple:
    segment = _segment(
        session_id=session_id,
        segment_id=segment_id,
        text=text,
        received_at_ms=1_000,
    )
    projection = ProjectionSegment(
        session_id=session_id,
        segment_id=segment.segment_id,
        revision=segment.revision,
        track_id=segment.track_id,
        language=segment.language,
        raw_text=segment.raw_text,
        display_text=segment.display_text,
        audio_start_ms=segment.audio_start_ms,
        audio_end_ms=segment.audio_end_ms,
        confidence=segment.confidence,
        received_at_ms=segment.received_at_ms,
        finalized_at=segment.finalized_at,
        updated_at=segment.updated_at,
    )
    evidence = caption_evidence_message(projection)
    content = ActionCandidateContent(
        title=GroundedValue[str](
            value="数据库迁移",
            origin="meeting_explicit",
            resolution="known",
            evidence_message_ids=(evidence.message_id,),
            confidence=0.99,
        ),
        deliverable=GroundedValue[str](
            value="完成数据库迁移",
            origin="meeting_explicit",
            resolution="known",
            evidence_message_ids=(evidence.message_id,),
            confidence=0.99,
        ),
        assignee=GroundedValue(
            value=None,
            origin="meeting_explicit",
            resolution="missing",
            evidence_message_ids=(evidence.message_id,),
        ),
        due_at=GroundedValue(
            value=None,
            origin="meeting_explicit",
            resolution="missing",
            evidence_message_ids=(evidence.message_id,),
        ),
        priority=GroundedValue(
            value=None,
            origin="server_default",
            resolution="missing",
            evidence_message_ids=(),
        ),
        evidence_messages=(evidence,),
    )
    return segment, content


def _runner(database: Database, candidate_id: str, *, context_guard=None):
    adapter = FakeTaskSystemAdapter(team_id="team-agent")
    service = TaskSystemService(database, adapter)
    registry = ToolRegistry()
    register_task_tools(registry, service)
    analyzer = _SuccessfulSubagent()
    runner = ActionRunRunner(
        database,
        _TaskLifecyclePlanner(candidate_id, 1),
        registry,
        ToolExecutor(database, registry),
        SubagentCoordinator(
            database,
            {
                "evidence": analyzer,
                "conflict": analyzer,
                "linear_research": analyzer,
            },
        ),
        EvidenceBoundCritic(),
        invocation_policy=TaskSystemInvocationPolicy(service),
        context_guard=context_guard,
    )
    return runner, adapter


async def _pending_execution(database: Database, *, grant=None, capabilities=("task.create",)):
    session_id = str(uuid.uuid4())
    goal = "Create the release-readiness task"
    segment, content = _candidate_content(
        session_id,
        "approval-evidence-1",
        "Create a release-readiness checklist for Friday",
    )
    with database.session() as db_session:
        db_session.add(_session(session_id))
        db_session.flush()
        db_session.add(segment)
        candidate = MeetingStateRepository(db_session).create_candidate(
            session_id=session_id,
            content=content,
            readiness="recordable",
        )
        db_session.commit()
        candidate_id = candidate.id

    with database.session() as db_session:
        snapshot = await ContextBuilder(db_session).build(
            session_id=session_id,
            goal=goal,
            actor_id="user-1",
            persist=True,
        )
        repository = AssistantRepository(db_session)
        grant_id = None
        if grant == "mismatch":
            created = repository.create_grant(
                session_id=session_id,
                actor_id="user-1",
                goal=goal,
                capabilities=capabilities,
                resource_scope={"linear_team_id": "team-agent"},
                candidate_ids=(candidate_id,),
                max_side_effects=1,
                expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10),
                linear_team_id="team-agent",
            )
            grant_id = created.id
        execution = repository.create_execution(
            session_id=session_id,
            profile="action_run",
            goal=goal,
            snapshot_id=snapshot.snapshot_id,
            grant_id=grant_id,
        )
        db_session.commit()
        execution_id = execution.id
        snapshot_id = snapshot.snapshot_id
    return session_id, execution_id, candidate_id, snapshot_id


def _approval_client(database: Database):
    app = FastAPI()
    app.state.database = database
    app.state.settings = Settings(_env_file=None, assistant_enabled=True)
    scheduler = SimpleNamespace(resumed=[], resume=None, enqueue=None)

    async def resume(execution_id: str) -> None:
        scheduler.resumed.append(execution_id)

    scheduler.resume = resume
    scheduler.enqueue = resume
    app.state.assistant_runtime = SimpleNamespace(
        action_runtime=SimpleNamespace(scheduler=scheduler)
    )
    app.include_router(assistant_router)
    return TestClient(app), scheduler


def test_external_write_without_grant_pauses_for_approval() -> None:
    async def scenario() -> None:
        database = Database("sqlite://")
        database.create_schema()
        session_id, execution_id, candidate_id, _snapshot_id = await _pending_execution(database)
        runner, adapter = _runner(database, candidate_id)
        result = await runner.run(execution_id)
        assert result.status == "needs_input"
        assert adapter.task_count == 0
        with database.session() as db_session:
            repository = AssistantRepository(db_session)
            approval = repository.get_pending_action_approval(execution_id)
            assert approval is not None
            assert approval.capability == "task.create"
            assert approval.candidate_id == candidate_id
            assert approval.status == "pending"
            assert db_session.scalar(select(func.count(ActionGrantRecord.id))) == 0
            assert db_session.scalar(
                select(func.count(AssistantToolCallRecord.id)).where(
                    AssistantToolCallRecord.tool_name == "task.create"
                )
            ) == 0
            execution = repository.get_execution_required(execution_id)
            assert execution.error_code is None
        database.dispose()

    asyncio.run(scenario())


def test_approve_mints_scoped_grant_and_resumes_write() -> None:
    async def scenario() -> None:
        database = Database("sqlite://")
        database.create_schema()
        session_id, execution_id, candidate_id, _snapshot_id = await _pending_execution(database)
        runner, adapter = _runner(database, candidate_id)
        paused = await runner.run(execution_id)
        assert paused.status == "needs_input"
        client, scheduler = _approval_client(database)
        with database.session() as db_session:
            approval = AssistantRepository(db_session).get_pending_action_approval(execution_id)
            execution = AssistantRepository(db_session).get_execution_required(execution_id)
            approval_id = approval.id
            version = execution.state_version
        first = client.post(
            f"/api/assistant/executions/{execution_id}/approvals/{approval_id}/approve",
            json={
                "client_operation_id": "approve-1",
                "expected_state_version": version,
                "capability": "task.delete",
                "candidate_ids": ["forged-candidate"],
                "max_side_effects": 99,
            },
        )
        assert first.status_code == 422
        approved = client.post(
            f"/api/assistant/executions/{execution_id}/approvals/{approval_id}/approve",
            json={
                "client_operation_id": "approve-1",
                "expected_state_version": version,
            },
        )
        assert approved.status_code == 202
        payload = approved.json()["execution"]
        assert payload["grant_id"] is not None
        assert payload["pending_approval"]["status"] == "approved"
        assert payload["pending_approval"]["candidate_id"] == candidate_id
        replay = client.post(
            f"/api/assistant/executions/{execution_id}/approvals/{approval_id}/approve",
            json={
                "client_operation_id": "approve-1",
                "expected_state_version": version,
            },
        )
        assert replay.status_code == 202
        duplicate = client.post(
            f"/api/assistant/executions/{execution_id}/approvals/{approval_id}/approve",
            json={
                "client_operation_id": "approve-2",
                "expected_state_version": payload["state_version"],
            },
        )
        assert duplicate.status_code == 202
        with database.session() as db_session:
            grants = list(db_session.scalars(select(ActionGrantRecord)))
            assert len(grants) == 1
            grant = grants[0]
            assert grant.capabilities_json == ["task.create"]
            assert grant.candidate_ids_json == [candidate_id]
            assert grant.max_side_effects == 1
            assert grant.resource_scope_json == {"linear_team_id": "team-agent"}
        resumed = await runner.run(execution_id)
        assert resumed.status == "completed"
        assert adapter.task_count == 1
        assert execution_id in scheduler.resumed
        database.dispose()

    asyncio.run(scenario())


def test_reject_cancels_without_grant_or_side_effect() -> None:
    async def scenario() -> None:
        database = Database("sqlite://")
        database.create_schema()
        session_id, execution_id, candidate_id, _snapshot_id = await _pending_execution(database)
        runner, adapter = _runner(database, candidate_id)
        paused = await runner.run(execution_id)
        assert paused.status == "needs_input"
        client, _scheduler = _approval_client(database)
        with database.session() as db_session:
            approval = AssistantRepository(db_session).get_pending_action_approval(execution_id)
            execution = AssistantRepository(db_session).get_execution_required(execution_id)
            approval_id = approval.id
            version = execution.state_version
        rejected = client.post(
            f"/api/assistant/executions/{execution_id}/approvals/{approval_id}/reject",
            json={
                "client_operation_id": "reject-1",
                "expected_state_version": version,
            },
        )
        assert rejected.status_code == 200
        body = rejected.json()["execution"]
        assert body["status"] == "cancelled"
        assert body["error_code"] is None
        assert body["pending_approval"]["status"] == "rejected"
        assert body["result"]["user_rejected"] is True
        with database.session() as db_session:
            assert db_session.scalar(select(func.count(ActionGrantRecord.id))) == 0
        assert adapter.task_count == 0
        database.dispose()

    asyncio.run(scenario())


def test_freshness_check_still_runs_after_approval() -> None:
    async def scenario() -> None:
        database = Database("sqlite://")
        database.create_schema()
        session_id, execution_id, candidate_id, _snapshot_id = await _pending_execution(database)
        inner = RelevantContextGuard(database)
        forced = {"count": 0}

        class _ForceChangeOnce:
            async def check(self, *, execution, snapshot):
                result = await inner.check(execution=execution, snapshot=snapshot)
                if execution.grant_id and forced["count"] == 0:
                    forced["count"] += 1
                    return FreshnessCheck(
                        snapshot=result.snapshot,
                        changed=True,
                        writable=True,
                        reason="relevant_context_changed",
                    )
                return result

        runner, adapter = _runner(
            database,
            candidate_id,
            context_guard=_ForceChangeOnce(),
        )
        paused = await runner.run(execution_id)
        assert paused.status == "needs_input"
        client, _scheduler = _approval_client(database)
        with database.session() as db_session:
            approval = AssistantRepository(db_session).get_pending_action_approval(execution_id)
            execution = AssistantRepository(db_session).get_execution_required(execution_id)
            approval_id = approval.id
            version = execution.state_version
        approved = client.post(
            f"/api/assistant/executions/{execution_id}/approvals/{approval_id}/approve",
            json={
                "client_operation_id": "approve-fresh",
                "expected_state_version": version,
            },
        )
        assert approved.status_code == 202
        resumed = await runner.run(execution_id)
        assert resumed.status in {"completed", "partial"}
        assert forced["count"] == 1
        assert adapter.task_count == 1
        with database.session() as db_session:
            events = [
                row.event_type
                for row in db_session.scalars(
                    select(AssistantEventRecord)
                    .where(AssistantEventRecord.execution_id == execution_id)
                    .order_by(AssistantEventRecord.id)
                )
            ]
            assert "action.context_refreshed" in events
            assert events.index("action.approval_granted") < events.index(
                "action.context_refreshed"
            )
        database.dispose()

    asyncio.run(scenario())


def test_grant_capability_mismatch_still_blocked_by_guard() -> None:
    async def scenario() -> None:
        database = Database("sqlite://")
        database.create_schema()
        _session_id, execution_id, candidate_id, _snapshot_id = await _pending_execution(
            database,
            grant="mismatch",
            capabilities=("task.search",),
        )
        runner, adapter = _runner(database, candidate_id)
        result = await runner.run(execution_id)
        assert result.status == "failed"
        assert adapter.task_count == 0
        with database.session() as db_session:
            assert db_session.scalar(
                select(func.count(AssistantToolCallRecord.id)).where(
                    AssistantToolCallRecord.tool_name == "task.create"
                )
            ) == 0
            assert db_session.scalar(
                select(func.count(AssistantActionApprovalRecord.id))
            ) == 0
            grant = db_session.scalar(select(ActionGrantRecord))
            assert grant is not None
            with pytest.raises(GrantAuthorizationError) as raised:
                authorize_tool_use(
                    action_grant_from_record(grant),
                    effect="external_write",
                    capability="task.create",
                    session_id=grant.session_id,
                    execution_goal=grant.goal,
                    candidate_id=candidate_id,
                    requested_resource_scope={"linear_team_id": "team-agent"},
                    now=utc_now(),
                )
            assert raised.value.code == "grant_capability_missing"
        database.dispose()

    asyncio.run(scenario())