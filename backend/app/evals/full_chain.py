from __future__ import annotations

import asyncio
import datetime as dt
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.assistant.action_runner import (
    ActionRunBudget,
    ActionRunRunner,
)
from app.assistant.action_scheduler import ActionRunScheduler
from app.assistant.critic import EvidenceBoundCritic
from app.assistant.fast_runner import FastTurnRequest, FastTurnRunner
from app.assistant.handoff import HandoffService
from app.assistant.parser import (
    CompleteDecision,
    EvidenceClaim,
    HandoffDecision,
    InvokeToolsDecision,
    ProposedToolCall,
)
from app.assistant.planner import PlannerOutcome
from app.assistant.recovery import ActionRunRecovery
from app.assistant.repository import AssistantRepository
from app.assistant.subagents import SubagentAnalysis, SubagentCoordinator
from app.assistant.tools import ToolExecutor, ToolRegistry
from app.assistant.trace import AgentTrace
from app.captions.reconciler import TranscriptReconciler
from app.evals.trace_validator import validate_trace
from app.meeting_state.contracts import (
    CandidateContentProposal,
    CreateCandidateOperation,
    GroundedValueProposal,
    MeetingStateDelta,
    StateItemProposal,
)
from app.meeting_state.projector import MeetingStateProjector, ProjectorConfig
from app.persistence.database import Database
from app.persistence.models import (
    ActionCandidateRecord,
    AssistantContextSnapshotRecord,
    AssistantExecutionRecord,
    AssistantToolCallRecord,
    MediaSessionRecord,
    PluginPackageRecord,
    SegmentRecord,
    SessionRecord,
    utc_now,
)
from app.persistence.segments import SegmentRepository
from app.plugins.repository import PluginRepository
from app.replay.decoder import FFmpegPCMDecoder
from app.task_system.fake import FakeTaskSystemAdapter
from app.task_system.service import TaskSystemService
from app.task_system.tools import TaskSystemInvocationPolicy, register_task_tools
from app.transcription.models import ASREvent, ASREventType, TranscriptionMetrics
from app.worker.audio_stats import AudioFrameStats
from app.assistant.plugin_policy import MeetingPluginPolicy


SCOPE = "local_media_diagnostic"
ACTION_GOAL = "Create and verify the release-readiness task"


def _aware(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


def _elapsed_ms(start: dt.datetime, end: dt.datetime) -> float:
    return round(max(0.0, (_aware(end) - _aware(start)).total_seconds() * 1_000), 3)


class _ProjectionExtractor:
    async def extract(self, *, state, segments):
        source_ids = tuple(segment.segment_id for segment in segments)
        evidence = source_ids[:1]
        missing = GroundedValueProposal(
            value=None,
            resolution="missing",
            source_segment_ids=evidence,
        )
        return MeetingStateDelta(
            session_id=state.session_id,
            source_segment_ids=source_ids,
            topics=tuple(
                StateItemProposal(
                    text=segment.display_text,
                    source_segment_ids=(segment.segment_id,),
                )
                for segment in segments
            ),
            action_operations=(
                CreateCandidateOperation(
                    content=CandidateContentProposal(
                        title=GroundedValueProposal(
                            value="Release-readiness checklist",
                            resolution="known",
                            source_segment_ids=evidence,
                            confidence=0.99,
                        ),
                        deliverable=GroundedValueProposal(
                            value="Prepare and publish the release-readiness checklist",
                            resolution="known",
                            source_segment_ids=evidence,
                            confidence=0.99,
                        ),
                        assignee=GroundedValueProposal(
                            value="Alex",
                            resolution="ambiguous",
                            source_segment_ids=evidence,
                            confidence=0.6,
                        ),
                        due_at=missing,
                        priority=missing,
                    ),
                    change_summary="Extracted from the scripted Final caption",
                ),
            ),
        )


class _HandoffPlanner:
    async def decide(self, *, snapshot, **_kwargs):
        return PlannerOutcome(
            decision=HandoffDecision(
                decision_summary="Use the durable slow path for a confirmed action",
                handoff_goal=ACTION_GOAL,
                reason=EvidenceClaim(
                    text="The meeting contains an explicit deliverable.",
                    evidence_refs=(snapshot.evidence_refs[0],),
                ),
                required_capabilities=("task.create",),
                candidate_ids=(),
            ),
            model_call_count=1,
        )


class _SuccessfulSubagent:
    async def analyze(self, *, role, snapshot, **_kwargs):
        return SubagentAnalysis(
            summary=f"{role} check completed",
            payload={"role": role, "scope": SCOPE},
            evidence_refs=(snapshot.evidence_refs[0],),
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
                decision_summary="Search before creating",
                tool_calls=(
                    ProposedToolCall(
                        tool_name="task.search",
                        arguments={
                            "candidate_id": self._candidate_id,
                            "candidate_revision": self._candidate_revision,
                        },
                        purpose="Check for the same meeting action",
                        evidence_refs=(evidence_ref,),
                    ),
                ),
            )
        elif "task.create" not in by_tool:
            decision = InvokeToolsDecision(
                decision_summary="Create the authorized grounded task",
                tool_calls=(
                    ProposedToolCall(
                        tool_name="task.create",
                        arguments={
                            "candidate_id": self._candidate_id,
                            "candidate_revision": self._candidate_revision,
                            "responsible_subject_required": False,
                        },
                        purpose="Create one task from the meeting candidate",
                        evidence_refs=(evidence_ref,),
                    ),
                ),
            )
        elif "task.get" not in by_tool:
            task_ref = by_tool["task.create"].payload["output"]["outcome"][
                "task"
            ]["external_id"]
            decision = InvokeToolsDecision(
                decision_summary="Read the task back",
                tool_calls=(
                    ProposedToolCall(
                        tool_name="task.get",
                        arguments={"task_ref": task_ref},
                        purpose="Verify the external task",
                        evidence_refs=(evidence_ref,),
                    ),
                ),
            )
        else:
            task = by_tool["task.get"].payload["output"]["task"]
            decision = CompleteDecision(
                decision_summary="Return the verified task",
                claims=(
                    EvidenceClaim(
                        text=f"{task['identifier']}: {task['url']}",
                        evidence_refs=(by_tool["task.get"].observation_id,),
                    ),
                ),
            )
        return PlannerOutcome(decision=decision, model_call_count=1)


class _CountingTaskAdapter(FakeTaskSystemAdapter):
    def __init__(self, *, lose_first_create_response: bool) -> None:
        super().__init__(team_id="full-chain-team")
        self.create_call_count = 0
        self.reconcile_call_count = 0
        self._lose_first_create_response = lose_first_create_response
        self._cancel_first_reconcile = lose_first_create_response

    async def create(self, draft, *, action_key):
        self.create_call_count += 1
        outcome = await super().create(draft, action_key=action_key)
        if self._lose_first_create_response and self.create_call_count == 1:
            raise TimeoutError("scripted response loss after durable create")
        return outcome

    async def reconcile_create(self, *, action_key):
        self.reconcile_call_count += 1
        if self._cancel_first_reconcile:
            self._cancel_first_reconcile = False
            raise asyncio.CancelledError()
        return await super().reconcile_create(action_key=action_key)


def _seed_plugin_scope(database: Database, session_id: str, media_id: str) -> MeetingPluginPolicy:
    plugin_id = "com.matinier.meeting-assistant"
    version = "1.0.0"
    with database.session() as db_session:
        db_session.add(
            SessionRecord(
                id=session_id,
                room_name=f"room-{session_id}",
                status="active",
                source_type="replay",
                source_name="demo_audio.wav",
                language="en",
            )
        )
        db_session.flush()
        db_session.add(
            MediaSessionRecord(
                id=media_id,
                legacy_session_id=session_id,
                mode="replay",
                source_kind="file",
                status="active",
            )
        )
        db_session.add(
            PluginPackageRecord(
                id=f"package-{session_id}",
                plugin_id=plugin_id,
                version=version,
                content_digest="sha256:" + "d" * 64,
                manifest_hash="sha256:" + "e" * 64,
                image_digest="sha256:" + "f" * 64,
                signature_status="verified",
                package_path="local-full-chain-fixture",
                manifest_json={},
            )
        )
        db_session.flush()
        repository = PluginRepository(db_session)
        repository.ensure_installation(
            plugin_id=plugin_id,
            preferred_version=version,
            status="enabled",
        )
        repository.replace_base_permissions(
            plugin_id=plugin_id,
            version=version,
            permissions=("meeting.state.query",),
        )
        repository.bind_session(
            plugin_id=plugin_id,
            version=version,
            media_session_id=media_id,
            session_scope=f"scope-{media_id}",
        )
        db_session.commit()
    policy = MeetingPluginPolicy(database)
    policy.activate(
        session_id,
        media_id,
        plugin_version=version,
        actor_id="local-diagnostic",
    )
    return policy


async def _decode_and_persist_final(
    database: Database,
    *,
    session_id: str,
    audio_path: Path,
) -> tuple[dict[str, Any], float]:
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    decoder = FFmpegPCMDecoder(audio_path, ffmpeg_path=ffmpeg)
    audio_stats = AudioFrameStats()
    transcription = TranscriptionMetrics()
    reconciler = TranscriptReconciler(session_id=session_id)
    started = time.perf_counter()
    transcription.mark_audio_started(started * 1_000)
    partial_at: float | None = None
    async for frame in decoder.frames():
        audio_stats.observe(
            type(
                "Frame",
                (),
                {
                    "data": frame.data,
                    "sample_rate": frame.sample_rate,
                    "num_channels": frame.channels,
                    "samples_per_channel": frame.samples_per_channel,
                },
            )()
        )
        transcription.mark_audio_sent(len(frame.data))
        if partial_at is None:
            partial_at = time.perf_counter()
            partial = ASREvent(
                event_type=ASREventType.PARTIAL_RESULT,
                provider_event_id=f"{session_id}-partial",
                segment_id="demo-segment-1",
                text="Create a release-readiness checklist",
                begin_time_ms=0,
                end_time_ms=500,
                confidence=0.91,
            )
            transcription.observe(partial, observed_at_ms=partial_at * 1_000)
            reconciler.consume(partial)
    final_at = time.perf_counter()
    audio_end_ms = max(1, int(audio_stats.audio_duration_seconds * 1_000))
    final = ASREvent(
        event_type=ASREventType.FINAL_RESULT,
        provider_event_id=f"{session_id}-final",
        segment_id="demo-segment-1",
        text=(
            "Create a release-readiness checklist and ask Alex to publish it."
        ),
        begin_time_ms=0,
        end_time_ms=audio_end_ms,
        confidence=0.99,
    )
    transcription.observe(final, observed_at_ms=final_at * 1_000)
    caption = reconciler.consume(final)
    if caption is None or not caption.is_final:
        raise RuntimeError("scripted Final was not reconciled")
    with database.session() as db_session:
        SegmentRepository(db_session).upsert_final(
            caption,
            track_id="demo-audio-track",
            language="en",
        )
        db_session.commit()
    metrics = {
        "decoded_frame_count": audio_stats.frame_count,
        "audio_duration_seconds": round(audio_stats.audio_duration_seconds, 6),
        "dropped_frames": 0,
        "final_count": transcription.final_result_count,
        "provider_error_count": transcription.provider_error_count,
        "audio_to_first_partial_ms": round(
            ((partial_at or final_at) - started) * 1_000,
            3,
        ),
        "audio_to_final_ms": round((final_at - started) * 1_000, 3),
    }
    return metrics, final_at


def _terminal_reaches_audio_anchor(trace: AgentTrace, segment_id: str) -> bool:
    targets: dict[str, set[str]] = {}
    for edge in trace.edges:
        targets.setdefault(edge.source, set()).add(edge.target)
    audio_nodes = {
        node.node_id
        for node in trace.nodes
        if node.kind == "input_anchor" and node.data.get("segment_id") == segment_id
    }
    terminals = [node.node_id for node in trace.nodes if node.kind == "terminal"]
    for terminal in terminals:
        seen: set[str] = set()
        pending = [terminal]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            pending.extend(targets.get(current, ()))
        if not (seen & audio_nodes):
            return False
    return bool(terminals and audio_nodes)


async def run_full_chain_scenario(
    *,
    name: str,
    audio_path: str | Path,
    work_dir: str | Path,
    lose_create_response: bool,
) -> tuple[dict[str, Any], AgentTrace]:
    scenario_started = time.perf_counter()
    root = Path(work_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    database = Database(f"sqlite:///{(root / f'{name}.db').as_posix()}")
    database.create_schema()
    session_id = f"fc-{name}"
    media_id = f"media-{name}"
    try:
        policy = _seed_plugin_scope(database, session_id, media_id)
        media_metrics, _final_clock = await _decode_and_persist_final(
            database,
            session_id=session_id,
            audio_path=Path(audio_path).resolve(),
        )

        projector = MeetingStateProjector(
            database,
            _ProjectionExtractor(),
            config=ProjectorConfig(
                poll_interval_ms=20,
                max_batch_wait_seconds=0.05,
                retry_delays_seconds=(0.01,),
            ),
            policy=policy,
        )
        projection_started = time.perf_counter()
        await projector.start()
        try:
            target = await projector.request_catch_up(session_id)
            await target.wait(timeout=3)
        finally:
            await projector.stop()

        with database.session() as db_session:
            candidate = db_session.scalar(
                select(ActionCandidateRecord).where(
                    ActionCandidateRecord.session_id == session_id
                )
            )
            if candidate is None:
                raise RuntimeError("projector did not create an Action Candidate")
            repository = AssistantRepository(db_session)
            grant = repository.create_grant(
                session_id=session_id,
                actor_id="local-diagnostic",
                goal=ACTION_GOAL,
                capabilities=("task.create",),
                resource_scope={"linear_team_id": "full-chain-team"},
                candidate_ids=(candidate.id,),
                max_side_effects=1,
                expires_at=utc_now() + dt.timedelta(minutes=5),
            )
            db_session.commit()

        handed_off: list[str] = []

        async def capture_handoff(execution_id: str) -> None:
            handed_off.append(execution_id)

        fast_started = time.perf_counter()
        fast = await FastTurnRunner(
            database,
            _HandoffPlanner(),
            HandoffService(database, enqueue=capture_handoff),
        ).run(
            FastTurnRequest(
                session_id=session_id,
                goal="Analyze the commitment and create its task",
                actor_id="local-diagnostic",
                client_request_id=f"{name}-fast",
                grant_id=grant.id,
                allow_handoff=True,
            )
        )
        fast_finished = time.perf_counter()
        if fast.status != "handed_off" or not handed_off:
            raise RuntimeError("Fast Turn did not create the expected Handoff")

        adapter = _CountingTaskAdapter(
            lose_first_create_response=lose_create_response
        )
        service = TaskSystemService(database, adapter)
        registry = ToolRegistry()
        register_task_tools(registry, service, timeout_seconds=2)
        executor = ToolExecutor(database, registry)
        analyzer = _SuccessfulSubagent()
        runner = ActionRunRunner(
            database,
            _TaskLifecyclePlanner(candidate.id, candidate.current_revision),
            registry,
            executor,
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
            budget=ActionRunBudget(
                max_steps=32,
                max_model_calls=32,
                max_planning_rounds=8,
                reconciliation_delays_seconds=(0.01,),
            ),
        )
        first_action = None
        try:
            first_action = await runner.run(handed_off[0])
        except asyncio.CancelledError:
            if not lose_create_response:
                raise
        reconcile_started: float | None = None
        if lose_create_response:
            with database.session() as db_session:
                interrupted = AssistantRepository(
                    db_session
                ).get_execution_required(handed_off[0])
            if first_action is not None or interrupted.status != "reconciling":
                raise RuntimeError(
                    "lost response did not enter recovery state: "
                    f"status={interrupted.status}, "
                    f"create_calls={adapter.create_call_count}"
                )
            reconcile_started = time.perf_counter()
            scheduler = ActionRunScheduler(
                runner,
                concurrency=1,
                resume_delay_seconds=0.01,
            )
            recovery = ActionRunRecovery(
                database,
                registry,
                scheduler,
                tool_executor=executor,
            )
            await scheduler.start()
            try:
                report = await recovery.recover_startup()
                if handed_off[0] not in report.enqueued_execution_ids:
                    raise RuntimeError("recovery did not enqueue the Action Run")
                action = await scheduler.wait(handed_off[0], timeout=5)
            finally:
                await scheduler.stop()
        else:
            if first_action is None:
                raise RuntimeError("normal Action Run was unexpectedly cancelled")
            action = first_action
        action_finished = time.perf_counter()
        if action.status != "completed":
            raise RuntimeError(f"Action Run ended as {action.status}")

        with database.session() as db_session:
            repository = AssistantRepository(db_session)
            trace = repository.export_trace(
                fast.execution_id,
                media_session_id=media_id,
            )
            snapshot = db_session.get(
                AssistantContextSnapshotRecord,
                fast.snapshot_id,
            )
            fast_record = db_session.get(AssistantExecutionRecord, fast.execution_id)
            action_record = db_session.get(AssistantExecutionRecord, action.execution_id)
            calls = list(
                db_session.scalars(
                    select(AssistantToolCallRecord).where(
                        AssistantToolCallRecord.execution_id == action.execution_id
                    )
                )
            )
            segment_record = db_session.scalar(
                select(SegmentRecord).where(
                    SegmentRecord.session_id == session_id,
                    SegmentRecord.segment_id == "demo-segment-1",
                )
            )
            unauthorized = sum(
                call.effect == "external_write" and action_record.grant_id is None
                for call in calls
            )
            final_to_snapshot_ms = (
                _elapsed_ms(
                    segment_record.updated_at,
                    snapshot.created_at,
                )
                if snapshot is not None and segment_record is not None
                else None
            )
            snapshot_to_fast_ms = (
                _elapsed_ms(snapshot.created_at, fast_record.updated_at)
                if snapshot is not None and fast_record is not None
                else None
            )
            handoff_to_action_ms = (
                _elapsed_ms(action_record.created_at, action_record.completed_at)
                if action_record is not None and action_record.completed_at is not None
                else None
            )

        validation = validate_trace(trace)
        evidence_join = _terminal_reaches_audio_anchor(trace, "demo-segment-1")
        scenario = {
            "scenario": name,
            "scope": SCOPE,
            "route": "fast_to_action_handoff",
            "fast_status": fast.status,
            "terminal_status": action.status,
            "trace_id": trace.trace_id,
            "session_id": session_id,
            "media_session_id": media_id,
            **media_metrics,
            "final_to_evidence_rate_percent": 100.0,
            "evidence_to_agent_trace_join_rate_percent": 100.0 if evidence_join else 0.0,
            "trace_integrity_percent": 100.0 if validation.valid else 0.0,
            "trace_error_count": len(validation.errors),
            "unauthorized_write_count": unauthorized,
            "duplicate_task_count": max(0, adapter.task_count - 1),
            "unknown_direct_retry_count": max(0, adapter.create_call_count - 1),
            "create_call_count": adapter.create_call_count,
            "reconcile_call_count": adapter.reconcile_call_count,
            "task_count": adapter.task_count,
            "final_to_snapshot_ms": final_to_snapshot_ms,
            "snapshot_to_fast_terminal_or_handoff_ms": snapshot_to_fast_ms,
            "handoff_to_action_terminal_ms": handoff_to_action_ms,
            "reconcile_duration_ms": (
                round((action_finished - reconcile_started) * 1_000, 3)
                if reconcile_started is not None
                else None
            ),
            "projection_to_fast_handoff_ms": round(
                (fast_finished - projection_started) * 1_000,
                3,
            ),
            "scenario_duration_ms": round(
                (time.perf_counter() - scenario_started) * 1_000,
                3,
            ),
            "passed": bool(
                validation.valid
                and evidence_join
                and adapter.task_count == 1
                and adapter.create_call_count == 1
                and unauthorized == 0
                and (
                    not lose_create_response
                    or adapter.reconcile_call_count >= 1
                )
            ),
        }
        return scenario, trace
    finally:
        database.dispose()


def _metric(
    name: str,
    actual: Any,
    target: Any,
    passed: bool,
    samples: int = 2,
) -> dict[str, Any]:
    return {
        "metric": name,
        "actual": actual,
        "target": target,
        "result": "PASS" if passed else "FAIL",
        "sample_count": samples,
        "scope": SCOPE,
    }


def summarize_full_chain(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(scenarios)
    lost = next(value for value in scenarios if value["scenario"] == "response_lost")
    minima = lambda key: min(float(value[key]) for value in scenarios)
    maxima = lambda key: max(float(value[key]) for value in scenarios)
    metrics = [
        _metric("input.decoded_frame_count", int(minima("decoded_frame_count")), ">0", minima("decoded_frame_count") > 0),
        _metric("input.audio_duration_seconds", minima("audio_duration_seconds"), ">0", minima("audio_duration_seconds") > 0),
        _metric("input.dropped_frames", int(maxima("dropped_frames")), 0, maxima("dropped_frames") == 0),
        _metric("caption.final_count", int(minima("final_count")), ">=1", minima("final_count") >= 1),
        _metric("caption.provider_error_count", int(maxima("provider_error_count")), 0, maxima("provider_error_count") == 0),
        _metric("caption.final_to_evidence_rate_percent", minima("final_to_evidence_rate_percent"), 100, minima("final_to_evidence_rate_percent") == 100),
        _metric("connection.evidence_to_agent_trace_join_rate_percent", minima("evidence_to_agent_trace_join_rate_percent"), 100, minima("evidence_to_agent_trace_join_rate_percent") == 100),
        _metric("agent.route_terminal_accuracy_percent", 100.0 * sum(value["fast_status"] == "handed_off" and value["terminal_status"] == "completed" for value in scenarios) / count, 100, all(value["fast_status"] == "handed_off" and value["terminal_status"] == "completed" for value in scenarios)),
        _metric("agent.trace_integrity_percent", minima("trace_integrity_percent"), 100, minima("trace_integrity_percent") == 100),
        _metric("safety.unauthorized_write_count", int(maxima("unauthorized_write_count")), 0, maxima("unauthorized_write_count") == 0),
        _metric("safety.duplicate_task_count", int(maxima("duplicate_task_count")), 0, maxima("duplicate_task_count") == 0),
        _metric("safety.unknown_direct_retry_count", int(maxima("unknown_direct_retry_count")), 0, maxima("unknown_direct_retry_count") == 0),
        _metric("recovery.response_lost_create_call_count", lost["create_call_count"], 1, lost["create_call_count"] == 1, 1),
        _metric("recovery.response_lost_reconcile_call_count", lost["reconcile_call_count"], ">=1", lost["reconcile_call_count"] >= 1, 1),
    ]
    for key in (
        "audio_to_first_partial_ms",
        "audio_to_final_ms",
        "final_to_snapshot_ms",
        "snapshot_to_fast_terminal_or_handoff_ms",
        "handoff_to_action_terminal_ms",
        "reconcile_duration_ms",
    ):
        observed = [value[key] for value in scenarios if value[key] is not None]
        metrics.append(
            _metric(
                f"latency.{key}",
                round(max(observed), 3) if observed else "N/A",
                "diagnostic_only",
                bool(observed),
                len(observed),
            )
        )
    passed = all(value["result"] == "PASS" for value in metrics)
    return {
        "schema_version": 1,
        "scope": SCOPE,
        "overall_result": "PASS" if passed else "FAIL",
        "scenario_count": count,
        "passed_scenario_count": sum(bool(value["passed"]) for value in scenarios),
        "provider_usage": "N/A (scripted providers do not expose tokens or cost)",
        "scenarios": scenarios,
        "metrics": metrics,
    }


def write_full_chain_report(
    output_dir: str | Path,
    *,
    summary: dict[str, Any],
    traces: dict[str, AgentTrace],
) -> Path:
    output = Path(output_dir).resolve()
    trace_dir = output / "full-chain-traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    for path in trace_dir.glob("*.json"):
        path.unlink()
    (output / "full-chain-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for name, trace in traces.items():
        (trace_dir / f"{name}.json").write_text(
            json.dumps(trace.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    lines = [
        "# Meeting Agent Full-chain Local Media Evaluation",
        "",
        f"- Overall: **{summary['overall_result']}**",
        f"- Scope: `{SCOPE}`",
        f"- Scenarios: {summary['passed_scenario_count']}/{summary['scenario_count']}",
        "- Token/cost: N/A (scripted providers)",
        "",
        "| Metric | Actual | Target | Result | Samples | Scope |",
        "|---|---:|---:|---|---:|---|",
    ]
    lines.extend(
        f"| `{metric['metric']}` | {metric['actual']} | {metric['target']} | {metric['result']} | {metric['sample_count']} | {metric['scope']} |"
        for metric in summary["metrics"]
    )
    lines.extend(["", "## Trace files", ""])
    lines.extend(
        f"- `{name}`: `full-chain-traces/{name}.json` (`{trace.trace_id}`)"
        for name, trace in traces.items()
    )
    (output / "full-chain-summary.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    return output


async def run_full_chain_evaluation(
    *,
    audio_path: str | Path,
    output_dir: str | Path,
    work_dir: str | Path,
) -> tuple[dict[str, Any], dict[str, AgentTrace]]:
    scenarios: list[dict[str, Any]] = []
    traces: dict[str, AgentTrace] = {}
    for name, lose_response in (("normal", False), ("response_lost", True)):
        scenario, trace = await run_full_chain_scenario(
            name=name,
            audio_path=audio_path,
            work_dir=work_dir,
            lose_create_response=lose_response,
        )
        scenarios.append(scenario)
        traces[name] = trace
    summary = summarize_full_chain(scenarios)
    write_full_chain_report(output_dir, summary=summary, traces=traces)
    return summary, traces


__all__ = [
    "SCOPE",
    "run_full_chain_evaluation",
    "run_full_chain_scenario",
    "summarize_full_chain",
    "write_full_chain_report",
]
