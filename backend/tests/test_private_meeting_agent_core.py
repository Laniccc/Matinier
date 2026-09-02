from __future__ import annotations

import asyncio
import datetime as dt
import uuid

from meeting_plugin_fakes import authorize_test_meeting

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.assistant.action_runner import ActionRunRunner
from app.assistant.action_scheduler import ActionRunScheduler
from app.assistant.bootstrap import _normalize_subagent_payload
from app.assistant.context import ContextBuilder
from app.assistant.critic import EvidenceBoundCritic
from app.assistant.fast_runner import (
    FastTurnRequest,
    FastTurnRunner,
)
from app.assistant.handoff import HandoffService
from app.assistant.parser import (
    CompleteDecision,
    EvidenceClaim,
    HandoffDecision,
    InvokeToolsDecision,
    ProposedToolCall,
    RespondDecision,
)
from app.assistant.planner import PlannerOutcome, PlanningObservation
from app.assistant.repository import AssistantRepository
from app.assistant.subagents import SubagentAnalysis, SubagentCoordinator
from app.assistant.tools import (
    ToolExecutionContext,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from app.meeting_state.contracts import MeetingStateDelta, StateItemProposal
from app.meeting_state.candidates import caption_evidence_message
from app.meeting_state.contracts import ProjectionSegment
from app.meeting_state.models import ActionCandidateContent, GroundedValue
from app.meeting_state.projector import MeetingStateProjector, ProjectorConfig
from app.meeting_state.repository import MeetingStateRepository
from app.persistence.database import Database
from app.persistence.models import (
    ActionGrantRecord,
    AssistantExecutionRecord,
    AssistantEventRecord,
    AssistantHandoffRecord,
    AssistantSubagentRunRecord,
    AssistantToolCallRecord,
    ExternalActionClaimRecord,
    MeetingProjectionOffsetRecord,
    SegmentRecord,
    SessionRecord,
)
from app.task_system.fake import FakeTaskSystemAdapter
from app.task_system.models import ExternalTask, TaskReconciliationResult
from app.task_system.service import TaskSystemService, derive_task_create_key
from app.task_system.tools import (
    TaskSystemInvocationPolicy,
    register_task_tools,
)


def _session(session_id: str) -> SessionRecord:
    return SessionRecord(
        id=session_id,
        room_name=f"room-{session_id}",
        status="active",
        source_type="microphone",
        source_name="test microphone",
        language="zh",
    )


def _segment(
    *,
    session_id: str,
    segment_id: str,
    text: str,
    received_at_ms: int,
) -> SegmentRecord:
    now = dt.datetime.now(dt.UTC) + dt.timedelta(milliseconds=received_at_ms)
    return SegmentRecord(
        id=str(uuid.uuid4()),
        session_id=session_id,
        segment_id=segment_id,
        track_id="track-1",
        revision=1,
        language="zh",
        raw_text=text,
        display_text=text,
        audio_start_ms=received_at_ms,
        audio_end_ms=received_at_ms + 500,
        confidence=0.98,
        status="final",
        received_at_ms=received_at_ms,
        finalized_at=now,
        created_at=now,
        updated_at=now,
    )


class _StaticReconciliationAdapter(FakeTaskSystemAdapter):
    def __init__(self, *, team_id: str) -> None:
        super().__init__(team_id=team_id)
        self.matches: tuple[ExternalTask, ...] = ()

    async def reconcile_create(
        self,
        *,
        action_key: str,
    ) -> TaskReconciliationResult:
        matches = tuple(
            task for task in self.matches if task.action_key == action_key
        )
        status = "none" if not matches else (
            "single" if len(matches) == 1 else "multiple"
        )
        return TaskReconciliationResult(status=status, matches=matches)


class _TopicExtractor:
    async def extract(self, *, state, segments):
        return MeetingStateDelta(
            session_id=state.session_id,
            source_segment_ids=tuple(segment.segment_id for segment in segments),
            topics=tuple(
                StateItemProposal(
                    text=segment.display_text,
                    source_segment_ids=(segment.segment_id,),
                )
                for segment in segments
            ),
        )


def test_task_reconciliation_requires_action_key_and_normalized_title() -> None:
    async def scenario() -> None:
        database = Database("sqlite://")
        database.create_schema()
        session_id = "session-reconcile-title"
        segment = _segment(
            session_id=session_id,
            segment_id="reconcile-evidence",
            text="Prepare the release notes",
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

        def known(value: str) -> GroundedValue[str]:
            return GroundedValue[str](
                value=value,
                origin="meeting_explicit",
                resolution="known",
                evidence_message_ids=(evidence.message_id,),
            )

        missing = GroundedValue(
            value=None,
            origin="server_default",
            resolution="missing",
            evidence_message_ids=(),
        )
        with database.session() as db_session:
            db_session.add(_session(session_id))
            db_session.flush()
            db_session.add(segment)
            candidate = MeetingStateRepository(db_session).create_candidate(
                session_id=session_id,
                content=ActionCandidateContent(
                    title=known("Release Notes"),
                    deliverable=known("Publish the release notes"),
                    assignee=missing,
                    due_at=missing,
                    priority=missing,
                    evidence_messages=(evidence,),
                ),
                readiness="recordable",
            )
            db_session.commit()

        adapter = _StaticReconciliationAdapter(team_id="team-reconcile")
        service = TaskSystemService(database, adapter)
        action_key = service.action_key_for_candidate(
            candidate.id,
            expected_revision=1,
        )
        now = dt.datetime.now(dt.UTC)

        def task(identifier: str, title: str) -> ExternalTask:
            return ExternalTask(
                external_id=f"task-{identifier}",
                identifier=identifier,
                url=f"https://fake.tasks.local/{identifier}",
                title=title,
                status="active",
                team_id="team-reconcile",
                action_key=action_key,
                created_at=now,
                updated_at=now,
            )

        adapter.matches = (task("MATCH", "  release   NOTES "),)
        equivalent = await service.reconcile_candidate(
            candidate.id,
            expected_revision=1,
        )
        assert equivalent.status == "single"
        assert equivalent.matches[0].identifier == "MATCH"

        adapter.matches = (task("WRONG", "Deploy the application"),)
        mismatch = await service.reconcile_candidate(
            candidate.id,
            expected_revision=1,
        )
        assert mismatch.status == "none"
        assert mismatch.matches == ()

        adapter.matches = (
            task("DUP-1", "Release Notes"),
            task("DUP-2", "release notes"),
        )
        ambiguous = await service.reconcile_candidate(
            candidate.id,
            expected_revision=1,
        )
        assert ambiguous.status == "multiple"
        assert len(ambiguous.matches) == 2
        assert adapter.task_count == 0
        database.dispose()

    asyncio.run(scenario())


class _EmptyExtractor:
    async def extract(self, *, state, segments):
        return MeetingStateDelta(
            session_id=state.session_id,
            source_segment_ids=tuple(segment.segment_id for segment in segments),
        )


class _ProjectedAndTailPlanner:
    def __init__(self) -> None:
        self.snapshot = None

    async def decide(self, *, snapshot, **_kwargs):
        self.snapshot = snapshot
        captions = {
            message.segment_id: message.message_id
            for message in snapshot.evidence_messages
            if message.message_kind == "caption"
        }
        assert set(captions) == {"projected-1", "tail-1"}
        assert snapshot.state_slice["meeting_state"]["topics"][0]["text"] == (
            "The release candidate is approved"
        )
        return PlannerOutcome(
            decision=RespondDecision(
                decision_summary="Answer from projected state and current tail",
                claims=(
                    EvidenceClaim(
                        text="The release candidate was approved.",
                        evidence_refs=(captions["projected-1"],),
                    ),
                    EvidenceClaim(
                        text="The rollout starts tomorrow.",
                        evidence_refs=(captions["tail-1"],),
                    ),
                ),
            ),
            model_call_count=1,
        )


class _HandoffPlanner:
    async def decide(self, *, snapshot, **_kwargs):
        return PlannerOutcome(
            decision=HandoffDecision(
                decision_summary="Continue as a durable analysis",
                handoff_goal="Compare the meeting commitments in detail",
                reason=EvidenceClaim(
                    text="The request explicitly asks for longer analysis.",
                    evidence_refs=(snapshot.evidence_refs[0],),
                ),
                required_capabilities=(),
                candidate_ids=(),
            ),
            model_call_count=1,
        )


class _WaitToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(min_length=1)


class _WaitToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str = Field(min_length=1)


class _ControllableReadTool:
    provider_name = "test-read-tool"

    def __init__(
        self,
        *,
        first_started: asyncio.Event,
        release_first: asyncio.Event,
    ) -> None:
        self._first_started = first_started
        self._release_first = release_first

    async def execute(self, context: ToolExecutionContext) -> ToolResult:
        arguments = _WaitToolInput.model_validate(context.arguments)
        if arguments.label == "first":
            self._first_started.set()
            await self._release_first.wait()
        return ToolResult(
            status="succeeded",
            output={"value": arguments.label},
        )

    async def reconcile(self, context: ToolExecutionContext) -> ToolResult:
        arguments = _WaitToolInput.model_validate(context.arguments)
        return ToolResult(
            status="succeeded",
            output={"value": arguments.label},
        )


class _SuccessfulSubagent:
    async def analyze(self, *, role, snapshot, **_kwargs):
        return SubagentAnalysis(
            summary=f"{role} branch completed",
            payload={"role": role},
            evidence_refs=(snapshot.evidence_refs[0],),
        )


class _IndependentActionPlanner:
    async def decide(self, *, goal, snapshot, observations, **_kwargs):
        tool_observation = next(
            (
                observation
                for observation in observations
                if observation.payload.get("tool_name") == "test.wait"
            ),
            None,
        )
        if tool_observation is None:
            label = "first" if "first" in goal else "second"
            return PlannerOutcome(
                decision=InvokeToolsDecision(
                    decision_summary=f"Run the independent {label} read",
                    tool_calls=(
                        ProposedToolCall(
                            tool_name="test.wait",
                            arguments={"label": label},
                            purpose=f"Complete the {label} independent action",
                            evidence_refs=(snapshot.evidence_refs[0],),
                        ),
                    ),
                ),
                model_call_count=1,
            )
        return PlannerOutcome(
            decision=CompleteDecision(
                decision_summary="Complete from the independent tool result",
                claims=(
                    EvidenceClaim(
                        text=str(tool_observation.payload["output"]["value"]),
                        evidence_refs=(tool_observation.observation_id,),
                    ),
                ),
            ),
            model_call_count=1,
        )


class _TaskLifecyclePlanner:
    def __init__(self, candidate_id: str, candidate_revision: int) -> None:
        self._candidate_id = candidate_id
        self._candidate_revision = candidate_revision

    async def decide(self, *, snapshot, observations, **_kwargs):
        by_tool = {
            observation.payload.get("tool_name"): observation
            for observation in observations
            if observation.payload.get("tool_name") is not None
        }
        evidence_ref = next(
            message.message_id
            for message in snapshot.evidence_messages
            if message.message_kind == "caption"
        )
        if "task.search" not in by_tool:
            decision = InvokeToolsDecision(
                decision_summary="Search for an existing task first",
                tool_calls=(
                    ProposedToolCall(
                        tool_name="task.search",
                        arguments={
                            "candidate_id": self._candidate_id,
                            "candidate_revision": self._candidate_revision,
                        },
                        purpose="Find an existing task for this meeting action",
                        evidence_refs=(evidence_ref,),
                    ),
                ),
            )
        elif "task.create" not in by_tool:
            decision = InvokeToolsDecision(
                decision_summary="Create the grounded task after research",
                tool_calls=(
                    ProposedToolCall(
                        tool_name="task.create",
                        arguments={
                            "candidate_id": self._candidate_id,
                            "candidate_revision": self._candidate_revision,
                            "responsible_subject_required": False,
                        },
                        purpose="Create one task from the recordable Candidate",
                        evidence_refs=(evidence_ref,),
                    ),
                ),
            )
        elif "task.get" not in by_tool:
            task_ref = by_tool["task.create"].payload["output"]["outcome"][
                "task"
            ]["external_id"]
            decision = InvokeToolsDecision(
                decision_summary="Read the created task back",
                tool_calls=(
                    ProposedToolCall(
                        tool_name="task.get",
                        arguments={"task_ref": task_ref},
                        purpose="Verify the created task fields and URL",
                        evidence_refs=(evidence_ref,),
                    ),
                ),
            )
        else:
            task = by_tool["task.get"].payload["output"]["task"]
            decision = CompleteDecision(
                decision_summary="Return the verified external task",
                claims=(
                    EvidenceClaim(
                        text=f"{task['identifier']}: {task['url']}",
                        evidence_refs=(by_tool["task.get"].observation_id,),
                    ),
                ),
            )
        return PlannerOutcome(decision=decision, model_call_count=1)


def test_final_caption_projects_and_fast_ask_uses_tail() -> None:
    async def scenario() -> None:
        database = Database("sqlite://")
        database.create_schema()
        session_id = "session-fast-tail"
        with database.session() as db_session:
            db_session.add(_session(session_id))
            db_session.flush()
            db_session.add(
                _segment(
                    session_id=session_id,
                    segment_id="projected-1",
                    text="The release candidate is approved",
                    received_at_ms=1_000,
                )
            )
            db_session.commit()

        projector = MeetingStateProjector(
            database,
            _TopicExtractor(),
            config=ProjectorConfig(),
            policy=authorize_test_meeting(database, session_id),
        )
        backlog, _target = projector._read_session_backlog(session_id)
        await projector._buffer_segments(backlog)
        await projector._project_one_batch(session_id)

        with database.session() as db_session:
            assert db_session.scalar(
                select(func.count(MeetingProjectionOffsetRecord.id))
            ) == 1
            db_session.add(
                _segment(
                    session_id=session_id,
                    segment_id="tail-1",
                    text="The rollout starts tomorrow",
                    received_at_ms=2_000,
                )
            )
            db_session.commit()

        planner = _ProjectedAndTailPlanner()
        runner = FastTurnRunner(
            database,
            planner,
            HandoffService(database),
        )
        result = await runner.run(
            FastTurnRequest(
                session_id=session_id,
                goal="What was agreed and what happens next?",
                actor_id="user-1",
                client_request_id="fast-tail-1",
            )
        )

        assert result.status == "completed"
        assert result.response_text == (
            "The release candidate was approved.\n"
            "The rollout starts tomorrow."
        )
        cited = {
            evidence_ref
            for claim in result.claims
            for evidence_ref in claim.evidence_refs
        }
        assert planner.snapshot is not None
        caption_ids = {
            message.message_id
            for message in planner.snapshot.evidence_messages
            if message.message_kind == "caption"
        }
        assert cited == caption_ids
        database.dispose()

    asyncio.run(scenario())


def test_projector_skips_invalid_legacy_final_without_blocking_backlog() -> None:
    async def scenario() -> None:
        database = Database("sqlite://")
        database.create_schema()
        session_id = "session-invalid-legacy"
        with database.session() as db_session:
            db_session.add(_session(session_id))
            db_session.flush()
            db_session.add_all(
                [
                    _segment(
                        session_id=session_id,
                        segment_id="legacy-empty",
                        text="",
                        received_at_ms=500,
                    ),
                    _segment(
                        session_id=session_id,
                        segment_id="valid-final",
                        text="Ship the release candidate",
                        received_at_ms=1_000,
                    ),
                ]
            )
            db_session.commit()

        projector = MeetingStateProjector(
            database,
            _TopicExtractor(),
            config=ProjectorConfig(),
            policy=authorize_test_meeting(database, session_id),
        )
        backlog, target_revisions = projector._read_session_backlog(session_id)

        assert [segment.segment_id for segment in backlog] == ["valid-final"]
        assert target_revisions == {"legacy-empty": 1, "valid-final": 1}
        with database.session() as db_session:
            skipped = db_session.scalar(
                select(MeetingProjectionOffsetRecord).where(
                    MeetingProjectionOffsetRecord.session_id == session_id,
                    MeetingProjectionOffsetRecord.segment_id == "legacy-empty",
                )
            )
            assert skipped is not None
            assert skipped.processed_revision == 1

        await projector._buffer_segments(backlog)
        await projector._project_one_batch(session_id)
        with database.session() as db_session:
            assert db_session.scalar(
                select(func.count(MeetingProjectionOffsetRecord.id)).where(
                    MeetingProjectionOffsetRecord.session_id == session_id,
                )
            ) == 2
        database.dispose()

    asyncio.run(scenario())


def test_fast_ask_keeps_recent_finals_when_projected_state_is_empty() -> None:
    async def scenario() -> None:
        database = Database("sqlite://")
        database.create_schema()
        session_id = "session-empty-projection"
        with database.session() as db_session:
            db_session.add(_session(session_id))
            db_session.flush()
            db_session.add(
                _segment(
                    session_id=session_id,
                    segment_id="recent-final",
                    text="The launch review starts tomorrow",
                    received_at_ms=1_000,
                )
            )
            db_session.commit()

        projector = MeetingStateProjector(
            database,
            _EmptyExtractor(),
            config=ProjectorConfig(),
            policy=authorize_test_meeting(database, session_id),
        )
        backlog, _target = projector._read_session_backlog(session_id)
        await projector._buffer_segments(backlog)
        await projector._project_one_batch(session_id)

        with database.session() as db_session:
            snapshot = await ContextBuilder(db_session).build(
                session_id=session_id,
                goal="Summarize the meeting",
                fast_ask=True,
                persist=False,
            )

        caption_messages = [
            message
            for message in snapshot.evidence_messages
            if message.message_kind == "caption"
        ]
        assert [message.segment_id for message in caption_messages] == [
            "recent-final"
        ]
        assert snapshot.state_slice["tail_segments"][0]["display_text"] == (
            "The launch review starts tomorrow"
        )
        database.dispose()

    asyncio.run(scenario())


def test_browser_tab_course_captions_remain_private_assistant_evidence() -> None:
    async def scenario() -> None:
        database = Database("sqlite://")
        database.create_schema()
        session_id = "course-browser-tab-evidence"
        with database.session() as db_session:
            record = _session(session_id)
            record.source_type = "browser-tab"
            record.source_name = "Course tab"
            record.language = "en-US"
            db_session.add(record)
            db_session.flush()
            db_session.add(
                _segment(
                    session_id=session_id,
                    segment_id="course-caption-1",
                    text="Gradient descent follows the negative gradient",
                    received_at_ms=1_000,
                )
            )
            db_session.commit()

        with database.session() as db_session:
            snapshot = await ContextBuilder(db_session).build(
                session_id=session_id,
                goal="Summarize the course explanation",
                persist=False,
            )
        course_evidence = [
            message
            for message in snapshot.evidence_messages
            if message.message_kind == "caption"
            and message.segment_id == "course-caption-1"
        ]
        assert len(course_evidence) == 1
        assert "negative gradient" in course_evidence[0].display_text
        database.dispose()

    asyncio.run(scenario())


def test_fast_turn_handoff_preserves_root_parent() -> None:
    async def scenario() -> None:
        database = Database("sqlite://")
        database.create_schema()
        session_id = "session-handoff"
        with database.session() as db_session:
            db_session.add(_session(session_id))
            db_session.commit()

        enqueued: list[str] = []

        async def enqueue_after_commit(execution_id: str) -> None:
            with database.session() as db_session:
                assert db_session.get(AssistantExecutionRecord, execution_id) is not None
                assert db_session.scalar(
                    select(func.count(AssistantHandoffRecord.id)).where(
                        AssistantHandoffRecord.target_execution_id == execution_id
                    )
                ) == 1
            enqueued.append(execution_id)

        handoff = HandoffService(database, enqueue=enqueue_after_commit)
        runner = FastTurnRunner(database, _HandoffPlanner(), handoff)
        result = await runner.run(
            FastTurnRequest(
                session_id=session_id,
                goal="Analyze these commitments more deeply",
                actor_id="user-1",
                client_request_id="handoff-1",
                allow_handoff=True,
            )
        )

        assert result.status == "handed_off"
        assert result.handoff_execution_id is not None
        assert enqueued == [result.handoff_execution_id]
        with database.session() as db_session:
            source = db_session.get(AssistantExecutionRecord, result.execution_id)
            target = db_session.get(
                AssistantExecutionRecord,
                result.handoff_execution_id,
            )
            assert source is not None and target is not None
            assert source.status == "handed_off"
            assert target.profile == "action_run"
            assert target.status == "queued"
            assert target.parent_execution_id == source.id
            assert target.root_execution_id == source.root_execution_id
        database.dispose()

    asyncio.run(scenario())


def test_two_action_runs_advance_independently() -> None:
    async def scenario() -> None:
        database = Database("sqlite://")
        database.create_schema()
        session_id = "session-independent-actions"
        with database.session() as db_session:
            db_session.add(_session(session_id))
            db_session.commit()

        execution_ids: list[str] = []
        with database.session() as db_session:
            repository = AssistantRepository(db_session)
            for label in ("first", "second"):
                snapshot = await ContextBuilder(db_session).build(
                    session_id=session_id,
                    goal=f"Run the {label} independent action",
                    actor_id="user-1",
                    persist=True,
                )
                execution = repository.create_execution(
                    session_id=session_id,
                    profile="action_run",
                    goal=f"Run the {label} independent action",
                    snapshot_id=snapshot.snapshot_id,
                    client_request_id=f"independent-{label}",
                )
                execution_ids.append(execution.id)
            db_session.commit()

        first_started = asyncio.Event()
        release_first = asyncio.Event()
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="test.wait",
                version="1",
                capability="test.read",
                effect="read",
                input_model=_WaitToolInput,
                output_model=_WaitToolOutput,
                timeout_seconds=5.0,
                supports_idempotency=False,
                supports_reconciliation=False,
            ),
            _ControllableReadTool(
                first_started=first_started,
                release_first=release_first,
            ),
        )
        analyzer = _SuccessfulSubagent()
        subagents = SubagentCoordinator(
            database,
            {
                "evidence": analyzer,
                "conflict": analyzer,
                "linear_research": analyzer,
            },
        )
        runner = ActionRunRunner(
            database,
            _IndependentActionPlanner(),
            registry,
            ToolExecutor(database, registry),
            subagents,
            EvidenceBoundCritic(),
        )
        scheduler = ActionRunScheduler(runner, concurrency=2)
        await scheduler.start()
        await scheduler.enqueue(execution_ids[0])
        await scheduler.enqueue(execution_ids[1])

        await asyncio.wait_for(first_started.wait(), timeout=2.0)
        second_result = await scheduler.wait(execution_ids[1], timeout=2.0)
        assert second_result.status == "completed"
        assert not release_first.is_set()
        with database.session() as db_session:
            first_running = db_session.get(
                AssistantExecutionRecord,
                execution_ids[0],
            )
            assert first_running is not None
            assert first_running.status == "executing"

        release_first.set()
        first_result = await scheduler.wait(execution_ids[0], timeout=2.0)
        assert first_result.status == "completed"
        await scheduler.stop()

        with database.session() as db_session:
            events = list(
                db_session.scalars(
                    select(AssistantEventRecord)
                    .where(
                        AssistantEventRecord.execution_id.in_(execution_ids)
                    )
                    .order_by(AssistantEventRecord.id)
                )
            )
            streams = {
                execution_id: [
                    event
                    for event in events
                    if event.execution_id == execution_id
                ]
                for execution_id in execution_ids
            }
            assert execution_ids[0] != execution_ids[1]
            assert all(streams.values())
            assert all(
                stream[-1].status == "completed"
                for stream in streams.values()
            )
            assert set(streams[execution_ids[0]]).isdisjoint(
                streams[execution_ids[1]]
            )
        database.dispose()

    asyncio.run(scenario())


def test_action_run_creates_reads_back_and_reuses_fake_task() -> None:
    async def scenario() -> None:
        database = Database("sqlite://")
        database.create_schema()
        session_id = "session-task-agent"
        segment = _segment(
            session_id=session_id,
            segment_id="task-evidence-1",
            text="Create a release-readiness checklist for Friday",
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
        candidate_content = ActionCandidateContent(
            title=GroundedValue[str](
                value="Release-readiness checklist",
                origin="meeting_explicit",
                resolution="known",
                evidence_message_ids=(evidence.message_id,),
                confidence=0.99,
            ),
            deliverable=GroundedValue[str](
                value="Prepare and publish the release-readiness checklist",
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
        goal = "Create the release-readiness task"
        client_request_id = "task-create-replay-1"
        with database.session() as db_session:
            db_session.add(_session(session_id))
            db_session.flush()
            db_session.add(segment)
            candidate = MeetingStateRepository(db_session).create_candidate(
                session_id=session_id,
                content=candidate_content,
                readiness="recordable",
            )
            db_session.commit()

        with database.session() as db_session:
            snapshot = await ContextBuilder(db_session).build(
                session_id=session_id,
                goal=goal,
                actor_id="user-1",
                persist=True,
            )
            repository = AssistantRepository(db_session)
            grant = repository.create_grant(
                session_id=session_id,
                actor_id="user-1",
                goal=goal,
                capabilities=("task.create",),
                resource_scope={"linear_team_id": "team-agent"},
                candidate_ids=(candidate.id,),
                max_side_effects=1,
                expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10),
            )
            execution = repository.create_execution(
                session_id=session_id,
                profile="action_run",
                goal=goal,
                snapshot_id=snapshot.snapshot_id,
                grant_id=grant.id,
                client_request_id=client_request_id,
            )
            db_session.commit()

        adapter = FakeTaskSystemAdapter(team_id="team-agent")
        service = TaskSystemService(database, adapter)
        registry = ToolRegistry()
        register_task_tools(registry, service)
        analyzer = _SuccessfulSubagent()
        runner = ActionRunRunner(
            database,
            _TaskLifecyclePlanner(candidate.id, 1),
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
        )
        first = await runner.run(execution.id)
        assert first.status == "completed"
        assert adapter.task_count == 1
        task = adapter.tasks[0]
        assert task.url in (first.response_text or "")

        with database.session() as db_session:
            replay = AssistantRepository(db_session).create_execution(
                session_id=session_id,
                profile="action_run",
                goal=goal,
                snapshot_id=snapshot.snapshot_id,
                grant_id=grant.id,
                client_request_id=client_request_id,
            )
            db_session.commit()
            assert replay.id == execution.id
        second = await runner.run(replay.id)
        assert second.status == "completed"
        assert second.response_text == first.response_text
        assert task.url in (second.response_text or "")
        assert adapter.task_count == 1

        action_key = derive_task_create_key("team-agent", candidate.lineage_root_id)
        with database.session() as db_session:
            assert db_session.scalar(
                select(func.count(AssistantToolCallRecord.id)).where(
                    AssistantToolCallRecord.tool_name == "task.create"
                )
            ) == 1
            claim = db_session.scalar(
                select(ExternalActionClaimRecord).where(
                    ExternalActionClaimRecord.logical_action_key == action_key
                )
            )
            persisted_grant = db_session.get(ActionGrantRecord, grant.id)
            assert claim is not None and claim.status == "succeeded"
            assert persisted_grant is not None
            assert persisted_grant.used_side_effects == 1
            subagent_runs = list(
                db_session.scalars(
                    select(AssistantSubagentRunRecord)
                    .where(
                        AssistantSubagentRunRecord.execution_id == execution.id
                    )
                    .order_by(
                        AssistantSubagentRunRecord.planning_round,
                        AssistantSubagentRunRecord.role,
                    )
                )
            )
            rounds_by_role = {
                role: [
                    record.planning_round
                    for record in subagent_runs
                    if record.role == role
                ]
                for role in ("evidence", "conflict", "linear_research")
            }
            assert rounds_by_role == {
                "evidence": [1],
                "conflict": [1],
                "linear_research": [1, 2],
            }
        database.dispose()

    asyncio.run(scenario())


def test_subagent_payload_normalizes_provider_bookkeeping_and_refs() -> None:
    normalized = _normalize_subagent_payload(
        {
            "summary": "Identity research completed.",
            "payload": {},
            "evidence_refs": ["caption-1", "user-observation-1"],
            "model_call_count": 0,
            "blocks_external_write": False,
        },
        available_evidence_refs=("caption-1",),
    )
    analysis = SubagentAnalysis.model_validate(normalized)
    assert analysis.model_call_count == 1
    assert analysis.evidence_refs == ("caption-1",)


def test_user_input_refreshes_blocking_subagent_branches_once() -> None:
    async def scenario() -> None:
        database = Database("sqlite://")
        database.create_schema()
        session_id = "session-user-input-refresh"
        with database.session() as db_session:
            db_session.add(_session(session_id))
            db_session.flush()
            db_session.add(
                _segment(
                    session_id=session_id,
                    segment_id="user-input-evidence-1",
                    text="小王负责整理会议纪要",
                    received_at_ms=1_000,
                )
            )
            db_session.commit()

        with database.session() as db_session:
            snapshot = await ContextBuilder(db_session).build(
                session_id=session_id,
                goal="创建会议纪要任务",
                actor_id="user-1",
                persist=True,
            )
            execution = AssistantRepository(db_session).create_execution(
                session_id=session_id,
                profile="action_run",
                goal="创建会议纪要任务",
                snapshot_id=snapshot.snapshot_id,
            )
            db_session.commit()

        analyzer = _SuccessfulSubagent()
        coordinator = SubagentCoordinator(
            database,
            {
                "evidence": analyzer,
                "conflict": analyzer,
                "linear_research": analyzer,
            },
        )
        coordinator._ensure_branches(
            execution_id=execution.id,
            planning_round=1,
            snapshot_id=snapshot.snapshot_id,
            observations=(),
        )
        with database.session() as db_session:
            repository = AssistantRepository(db_session)
            for record in repository.list_subagent_runs(execution.id):
                record = repository.transition_subagent_run(
                    record.id,
                    expected_status="queued",
                    status="running",
                )
                if record.role == "linear_research":
                    repository.transition_subagent_run(
                        record.id,
                        expected_status="running",
                        status="failed",
                        result={"model_call_count": 1},
                        error_code="subagent_failed",
                    )
                    continue
                repository.transition_subagent_run(
                    record.id,
                    expected_status="running",
                    status="completed",
                    result=SubagentAnalysis(
                        summary=f"{record.role} branch completed",
                        evidence_refs=(snapshot.evidence_refs[0],),
                        blocks_external_write=record.role == "evidence",
                    ).model_dump(mode="json"),
                )
            db_session.commit()
        coordinator._ensure_branches(
            execution_id=execution.id,
            planning_round=2,
            snapshot_id=snapshot.snapshot_id,
            observations=(
                PlanningObservation(
                    observation_id="user-confirmation-1",
                    source="user",
                    summary="User confirmed that the task may be unassigned.",
                    payload={"answer": "确认以未分配方式创建任务。"},
                    evidence_refs=snapshot.evidence_refs,
                ),
            ),
        )
        coordinator._ensure_branches(
            execution_id=execution.id,
            planning_round=3,
            snapshot_id=snapshot.snapshot_id,
            observations=(
                PlanningObservation(
                    observation_id="user-confirmation-1",
                    source="user",
                    summary="User confirmed that the task may be unassigned.",
                    payload={"answer": "确认以未分配方式创建任务。"},
                    evidence_refs=snapshot.evidence_refs,
                ),
            ),
        )

        with database.session() as db_session:
            records = AssistantRepository(db_session).list_subagent_runs(
                execution.id
            )
            rounds_by_role = {
                role: [
                    record.planning_round
                    for record in records
                    if record.role == role
                ]
                for role in ("evidence", "conflict", "linear_research")
            }
            assert rounds_by_role == {
                "evidence": [1, 2],
                "conflict": [1],
                "linear_research": [1, 2],
            }
            refreshed = [
                record
                for record in records
                if record.planning_round == 2
            ]
            assert {record.role for record in refreshed} == {
                "evidence",
                "linear_research",
            }
            assert all(
                record.budget_json["refresh_reason"]
                == "user_input_observed"
                for record in refreshed
            )
        database.dispose()

    asyncio.run(scenario())
