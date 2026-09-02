from __future__ import annotations

import datetime as dt
import time
import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.assistant.context import ContextBuilder
from app.assistant.models import HandoffEnvelope
from app.assistant.repository import AssistantRepository
from app.assistant.tools.executor import canonical_arguments_hash
from app.assistant.trace import AgentTrace
from app.evals.contracts import EvalCase
from app.evals.fakes import deterministic_id, digest
from app.evals.trace_validator import validate_trace
from app.persistence.database import Database
from app.persistence.models import SegmentRecord, SessionRecord


class FrozenTrialModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TrialResult(FrozenTrialModel):
    case_id: str
    trial: int = Field(ge=1)
    seed: int
    route: str
    terminal_status: str
    trace: AgentTrace
    duration_ms: float = Field(ge=0)
    model_calls: int = Field(ge=0)
    planning_rounds: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    tool_attempts: int = Field(ge=0)
    confirmed_side_effects: int = Field(ge=0)
    recovery_converged: bool


class ScenarioRunner:
    """Build scripted trials through the real DB, repository and state contracts."""

    def run(self, case: EvalCase, *, trial: int, seed: int) -> TrialResult:
        started = time.perf_counter()
        database = Database("sqlite://")
        database.create_schema()
        try:
            session_id = deterministic_id(seed, case.case_id, trial, "session")
            segment_id = case.input_evidence[0]
            timestamp = dt.datetime(2026, 9, 1, tzinfo=dt.UTC) + dt.timedelta(seconds=trial)
            with database.session() as db:
                db.add(SessionRecord(id=session_id, room_name=f"eval-{case.case_id}", status="active",
                    source_type="microphone", source_name="scripted-eval", language="zh"))
                db.flush()
                db.add(SegmentRecord(
                    id=deterministic_id(seed, case.case_id, trial, "segment"), session_id=session_id,
                    segment_id=segment_id, track_id="track-scripted", revision=1, language="zh",
                    raw_text=f"scripted evidence for {case.case_id}",
                    display_text=f"scripted evidence for {case.case_id}",
                    audio_start_ms=1000, audio_end_ms=1600, confidence=0.99, status="final",
                    received_at_ms=1000, finalized_at=timestamp, created_at=timestamp, updated_at=timestamp,
                ))
                db.commit()

            with database.session() as db:
                snapshot = ContextBuilder(db).build_sync(
                    session_id=session_id,
                    goal=f"scripted goal {case.case_id}",
                    actor_id="scripted-user",
                    persist=True,
                )
                db.commit()

            if case.route == "fast":
                root_id, terminal, model_calls, rounds = self._fast(database, case, snapshot, seed, trial)
            elif case.route == "handoff":
                root_id, terminal, model_calls, rounds = self._handoff(database, case, snapshot, seed, trial)
            else:
                root_id, terminal, model_calls, rounds = self._action(database, case, snapshot, seed, trial)
            with database.session() as db:
                trace = AssistantRepository(db).export_trace(root_id)
            report = validate_trace(trace)
            if not report.valid:
                codes = ", ".join(f"{error.node_id}:{error.error_code}" for error in report.errors)
                raise RuntimeError(f"scripted scenario produced invalid trace: {codes}")
            tool_nodes = [node for node in trace.nodes if node.kind == "tool"]
            return TrialResult(
                case_id=case.case_id, trial=trial, seed=seed, route=case.route,
                terminal_status=terminal, trace=trace,
                duration_ms=(time.perf_counter() - started) * 1000,
                model_calls=model_calls, planning_rounds=rounds,
                tool_calls=len(tool_nodes),
                tool_attempts=sum(int(node.data.get("attempt_count", 0)) for node in tool_nodes),
                confirmed_side_effects=sum(1 for node in tool_nodes
                    if node.data.get("effect") == "external_write" and node.data.get("status") == "succeeded"),
                recovery_converged=case.fault not in {"response_lost", "http_429", "http_5xx"}
                    or terminal in case.terminal_statuses,
            )
        finally:
            database.dispose()

    @staticmethod
    def _transition(repo, execution, statuses):
        for status in statuses:
            execution = repo.transition_execution(
                execution.id, expected_version=execution.state_version, target_status=status,
                event_type=f"scenario.{status}", summary=f"scripted {status}",
            )
        return execution

    def _fast(self, database, case, snapshot, seed, trial):
        with database.session() as db:
            repo = AssistantRepository(db)
            execution = repo.create_execution(
                execution_id=deterministic_id(seed, case.case_id, trial, "root"),
                session_id=snapshot.session_id, profile="fast_turn",
                goal=f"scripted fast {case.case_id}", snapshot_id=snapshot.snapshot_id,
                client_request_id=f"{case.case_id}:{trial}",
            )
            execution = self._transition(repo, execution, ("contextualizing", "deciding"))
            repo.append_step(execution_id=execution.id, kind="plan",
                input_payload={"round": 1}, output_payload={"decision": "respond"})
            execution = self._transition(repo, execution, ("responding", "completed"))
            db.commit()
            return execution.id, execution.status, 1, 1

    def _grant(self, repo, snapshot, case, seed, trial):
        return repo.create_grant(
            grant_id=deterministic_id(seed, case.case_id, trial, "grant"),
            session_id=snapshot.session_id, actor_id="scripted-user",
            goal=f"scripted action {case.case_id}", capabilities=("task.create",),
            resource_scope={"linear_team_id": "scripted-team"}, candidate_ids=("candidate-1",),
            max_side_effects=case.max_side_effects or 1,
            expires_at=dt.datetime(2030, 9, 2, tzinfo=dt.UTC), linear_team_id="scripted-team",
        )

    def _handoff(self, database, case, snapshot, seed, trial):
        with database.session() as db:
            repo = AssistantRepository(db)
            source = repo.create_execution(
                execution_id=deterministic_id(seed, case.case_id, trial, "root"),
                session_id=snapshot.session_id, profile="fast_turn",
                goal=f"scripted handoff {case.case_id}", snapshot_id=snapshot.snapshot_id,
                client_request_id=f"{case.case_id}:{trial}",
            )
            source = self._transition(repo, source, ("contextualizing", "deciding"))
            grant = self._grant(repo, snapshot, case, seed, trial) if case.expected_tool_effect == "external_write" else None
            target = repo.create_execution(
                execution_id=deterministic_id(seed, case.case_id, trial, "slow"),
                session_id=snapshot.session_id, profile="action_run",
                goal=f"scripted slow {case.case_id}", parent_execution_id=source.id,
                root_execution_id=source.root_execution_id, snapshot_id=snapshot.snapshot_id,
                grant_id=grant.id if grant else None,
            )
            envelope = HandoffEnvelope(
                handoff_id=deterministic_id(seed, case.case_id, trial, "handoff"),
                parent_execution_id=source.id, target_execution_id=target.id,
                session_id=snapshot.session_id, goal=f"scripted slow {case.case_id}",
                grant_id=grant.id if grant else None, context_snapshot_id=snapshot.snapshot_id,
                meeting_state_version=snapshot.meeting_state_version,
                evidence_refs=snapshot.evidence_refs, completed_steps=(), observations=(),
                unresolved_conflicts=(), candidate_plan={}, remaining_budget={"max_steps": 4},
                idempotency_scope=f"handoff:{source.id}",
            )
            repo.create_handoff(envelope)
            source = repo.transition_execution(source.id, expected_version=source.state_version,
                target_status="handed_off", event_type="handoff.committed", summary="scripted handoff",
                payload={"target_execution_id": target.id, "handoff_id": envelope.handoff_id})
            repo.append_event(execution_id=target.id, event_type="handoff.received",
                phase="queued", summary="scripted handoff received",
                payload={"source_execution_id": source.id, "handoff_id": envelope.handoff_id})
            db.commit()
        terminal, calls, rounds = self._run_action_execution(database, target.id, case, snapshot, seed, trial)
        return source.id, terminal, calls + 1, rounds + 1

    def _action(self, database, case, snapshot, seed, trial):
        with database.session() as db:
            repo = AssistantRepository(db)
            grant = self._grant(repo, snapshot, case, seed, trial) if case.expected_tool_effect == "external_write" else None
            execution = repo.create_execution(
                execution_id=deterministic_id(seed, case.case_id, trial, "root"),
                session_id=snapshot.session_id, profile="action_run",
                goal=f"scripted action {case.case_id}", snapshot_id=snapshot.snapshot_id,
                grant_id=grant.id if grant else None, client_request_id=f"{case.case_id}:{trial}",
            )
            db.commit()
        terminal, calls, rounds = self._run_action_execution(database, execution.id, case, snapshot, seed, trial)
        return execution.id, terminal, calls, rounds

    def _run_action_execution(self, database, execution_id, case, snapshot, seed, trial):
        with database.session() as db:
            repo = AssistantRepository(db)
            execution = repo.get_execution_required(execution_id)
            execution = self._transition(repo, execution, ("planning",))
            repo.append_step(execution_id=execution.id, kind="plan",
                input_payload={"round": 1}, output_payload={"route": case.route})
            branch = repo.create_subagent_run(
                subagent_run_id=deterministic_id(seed, case.case_id, trial, "subagent"),
                execution_id=execution.id, planning_round=1, role="evidence",
                task={"case_id": case.case_id}, budget={"max_model_calls": 1},
                snapshot_id=snapshot.snapshot_id,
            )
            branch = repo.transition_subagent_run(branch.id, expected_status="queued", status="running")
            repo.transition_subagent_run(branch.id, expected_status="running", status="completed",
                result={"model_call_count": 1, "evidence_refs": list(snapshot.evidence_refs),
                    "blocks_external_write": False, "summary": "scripted"})
            if case.route == "needs_input":
                execution = self._transition(repo, execution, ("needs_input",))
                db.commit()
                return execution.status, 1, 1
            execution = self._transition(repo, execution, ("executing",))
            if case.expected_tool_name:
                execution = self._tool(repo, execution, case, snapshot, seed, trial)
            else:
                execution = self._transition(repo, execution, ("observing",))
            terminal = "partial" if case.case_id in {"unresolved_assignee", "task_429_5xx"} else "completed"
            execution = self._transition(repo, execution, (terminal,))
            db.commit()
            return execution.status, 2, 1

    def _tool(self, repo, execution, case, snapshot, seed, trial):
        arguments = dict(case.expected_arguments_subset)
        arguments_hash = canonical_arguments_hash(arguments)
        external = case.expected_tool_effect == "external_write"
        call = repo.prepare_tool_call(
            tool_call_id=deterministic_id(seed, case.case_id, trial, "tool"),
            execution_id=execution.id, tool_name=case.expected_tool_name,
            tool_version="1", capability=case.expected_tool_name,
            effect=case.expected_tool_effect, arguments=arguments, arguments_hash=arguments_hash,
            logical_action_key=digest(case.case_id, "action") if external else None,
            idempotency_key=f"{case.case_id}:{trial}:write" if external else None,
        )
        claim = None
        if external:
            claim = repo.reserve_external_action_claim(
                claim_id=deterministic_id(seed, case.case_id, trial, "claim"),
                provider="scripted-task", capability=case.expected_tool_name,
                logical_action_key=call.logical_action_key, holder_execution_id=execution.id,
                arguments_hash=arguments_hash,
                lease_expires_at=dt.datetime(2030, 9, 2, tzinfo=dt.UTC), tool_call_id=call.id,
            )
            repo.consume_grant(execution.grant_id)
            claim = repo.transition_external_action_claim(claim.id,
                holder_execution_id=execution.id, expected_status="reserved", target_status="requesting")
        call = repo.update_tool_call(call.id, expected_status="prepared", status="requesting", increment_attempt=True)
        if case.fault == "response_lost":
            execution = self._transition(repo, execution, ("waiting_external",))
            call = repo.update_tool_call(call.id, expected_status="requesting", status="unknown",
                error_code="response_lost", error_message="Scripted response was lost")
            claim = repo.transition_external_action_claim(claim.id,
                holder_execution_id=execution.id, expected_status="requesting", target_status="unknown")
            execution = self._transition(repo, execution, ("reconciling",))
            call = repo.update_tool_call(call.id, expected_status="unknown", status="reconciling", increment_attempt=True)
            call = repo.update_tool_call(call.id, expected_status="reconciling", status="succeeded",
                result={"status": "succeeded", "output": {"reconciled": True}},
                external_reference={"identifier": f"FAKE-{trial}", "action_key": call.logical_action_key})
            repo.transition_external_action_claim(claim.id,
                holder_execution_id=execution.id, expected_status="unknown", target_status="succeeded",
                external_reference={"identifier": f"FAKE-{trial}", "action_key": call.logical_action_key})
            execution = self._transition(repo, execution, ("observing",))
        elif case.fault in {"http_429", "http_5xx"}:
            call = repo.update_tool_call(call.id, expected_status="requesting", status="failed",
                result={"status": "failed", "error_code": case.fault,
                    "error_message": "Scripted bounded provider error", "retryable": True},
                error_code=case.fault, error_message="Scripted bounded provider error")
            execution = self._transition(repo, execution, ("observing",))
        else:
            reference = {"identifier": f"FAKE-{trial}", "action_key": call.logical_action_key} if external else None
            call = repo.update_tool_call(call.id, expected_status="requesting", status="succeeded",
                result={"status": "succeeded", "output": {"ok": True}}, external_reference=reference)
            if claim is not None:
                repo.transition_external_action_claim(claim.id,
                    holder_execution_id=execution.id, expected_status="requesting", target_status="succeeded",
                    external_reference=reference)
            execution = self._transition(repo, execution, ("observing",))
        repo.append_observation(execution_id=execution.id, source="tool", source_ref=call.id,
            observation={"tool_name": call.tool_name, "status": call.status},
            evidence_refs=snapshot.evidence_refs)
        return execution


__all__ = ["ScenarioRunner", "TrialResult"]
