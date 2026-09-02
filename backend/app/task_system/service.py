from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from dataclasses import dataclass

from app.meeting_state.models import ActionCandidateContent
from app.meeting_state.repository import MeetingStateRepository
from app.persistence.database import Database
from app.persistence.models import (
    ActionCandidateRecord,
    ActionCandidateRevisionRecord,
)
from app.task_system.contracts import TaskSystemAdapter
from app.task_system.identity import IdentityResolver
from app.task_system.models import (
    ExternalTask,
    PersonMention,
    ResolvedIdentity,
    TaskDecisionContext,
    TaskDraft,
    TaskReconciliationResult,
    TaskSearchQuery,
    TaskSearchResult,
    TaskServiceOutcome,
)


_ACTION_KEY_PREFIX = "linear.issue.create:v1"
_LOGGER = logging.getLogger(__name__)


class TaskNeedsInput(RuntimeError):
    def __init__(self, code: str, question: str) -> None:
        self.code = code
        self.question = question
        super().__init__(question)


def derive_task_create_key(team_id: str, candidate_lineage_root_id: str) -> str:
    if not team_id.strip() or not candidate_lineage_root_id.strip():
        raise ValueError("team and Candidate lineage root are required")
    return hashlib.sha256(
        (
            _ACTION_KEY_PREFIX
            + team_id.strip()
            + candidate_lineage_root_id.strip()
        ).encode("utf-8")
    ).hexdigest()


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _tokens(value: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", value.casefold()))


def _same_due_date(left: dt.datetime, right: dt.datetime) -> bool:
    return left.date() == right.date()


@dataclass(frozen=True, slots=True)
class _CandidateSnapshot:
    candidate_id: str
    lineage_root_id: str
    session_id: str
    revision: int
    readiness: str
    content_status: str
    execution_status: str
    content: ActionCandidateContent


@dataclass(frozen=True, slots=True)
class _PreparedCandidate:
    candidate: _CandidateSnapshot
    draft: TaskDraft
    action_key: str
    unresolved_identity: ResolvedIdentity | None


class TaskSystemService:
    """Deterministic task workflow above a provider-neutral Adapter."""

    def __init__(
        self,
        database: Database,
        adapter: TaskSystemAdapter,
        *,
        identity_resolver: IdentityResolver | None = None,
    ) -> None:
        self._database = database
        self._adapter = adapter
        self._identity_resolver = identity_resolver or IdentityResolver(
            database,
            adapter,
        )

    @property
    def adapter(self) -> TaskSystemAdapter:
        return self._adapter

    @property
    def database(self) -> Database:
        return self._database

    def action_key_for_candidate(
        self,
        candidate_id: str,
        *,
        expected_revision: int | None = None,
    ) -> str:
        candidate = self._load_candidate(
            candidate_id,
            expected_revision=expected_revision,
        )
        return derive_task_create_key(
            self._adapter.connection.team_id,
            candidate.lineage_root_id,
        )

    async def search_candidate(
        self,
        candidate_id: str,
        *,
        expected_revision: int,
    ) -> TaskSearchResult:
        candidate = self._load_candidate(
            candidate_id,
            expected_revision=expected_revision,
        )
        title = candidate.content.title.value
        if candidate.content.title.resolution != "known" or title is None:
            raise TaskNeedsInput(
                "candidate_title_unresolved",
                "Please confirm the task title before searching.",
            )
        key = derive_task_create_key(
            self._adapter.connection.team_id,
            candidate.lineage_root_id,
        )
        exact = await self._adapter.search(TaskSearchQuery(action_key=key))
        if exact.tasks:
            return exact
        return await self._adapter.search(TaskSearchQuery(title=title))

    async def get(self, task_ref: str) -> ExternalTask:
        return await self._adapter.get(task_ref)

    async def execute_candidate(
        self,
        candidate_id: str,
        *,
        expected_revision: int,
        actor_id: str,
        decision_context: TaskDecisionContext,
        responsible_subject_required: bool = False,
    ) -> TaskServiceOutcome:
        prepared = await self._prepare_candidate(
            candidate_id,
            expected_revision=expected_revision,
            actor_id=actor_id,
            responsible_subject_required=responsible_subject_required,
        )
        exact = await self._adapter.search(
            TaskSearchQuery(action_key=prepared.action_key)
        )
        if len(exact.tasks) > 1:
            raise TaskNeedsInput(
                "duplicate_external_side_effect",
                "Multiple external tasks share this action key; inspect them before continuing.",
            )
        if len(exact.tasks) == 1:
            task = await self._adapter.get(exact.tasks[0].external_id)
            self._mark_candidate_executed(candidate_id)
            return TaskServiceOutcome(
                disposition="same_action",
                action_key=prepared.action_key,
                task=task,
                created=False,
                partial=prepared.unresolved_identity is not None,
                unresolved_identity=prepared.unresolved_identity,
            )

        similar = await self._adapter.search(
            TaskSearchQuery(title=prepared.draft.title)
        )
        disposition, reused, related_refs = self._classify(
            prepared.draft,
            similar.tasks,
            decision_context,
            team_id=self._adapter.connection.team_id,
        )
        if disposition == "ambiguous":
            raise TaskNeedsInput(
                "task_deduplication_ambiguous",
                "Choose whether to reuse the related task or create a new one.",
            )
        if reused is not None:
            task = await self._adapter.get(reused.external_id)
            self._mark_candidate_executed(candidate_id)
            return TaskServiceOutcome(
                disposition=disposition,
                action_key=prepared.action_key,
                task=task,
                created=False,
                partial=prepared.unresolved_identity is not None,
                unresolved_identity=prepared.unresolved_identity,
            )

        draft = prepared.draft.model_copy(
            update={"related_task_refs": related_refs}
        )
        self._mark_candidate_executing(candidate_id)
        created = await self._adapter.create(
            draft,
            action_key=prepared.action_key,
        )
        task = await self._adapter.get(created.task.external_id)
        self._verify_read_back(draft, task)
        self._mark_candidate_executed(candidate_id)
        return TaskServiceOutcome(
            disposition=disposition,
            action_key=prepared.action_key,
            task=task,
            created=created.created,
            partial=prepared.unresolved_identity is not None,
            unresolved_identity=prepared.unresolved_identity,
            related_task_refs=related_refs,
        )

    async def reconcile_candidate(
        self,
        candidate_id: str,
        *,
        expected_revision: int,
    ) -> TaskReconciliationResult:
        candidate = self._load_candidate(
            candidate_id,
            expected_revision=expected_revision,
        )
        action_key = derive_task_create_key(
            self._adapter.connection.team_id,
            candidate.lineage_root_id,
        )
        result = await self._adapter.reconcile_create(action_key=action_key)
        matches = tuple(
            task for task in result.matches if task.action_key == action_key
        )
        if len(matches) > 1:
            _LOGGER.warning(
                "task reconciliation found multiple action-key matches",
                extra={
                    "event": "task_reconciliation_ambiguous",
                    "candidate_id": candidate_id,
                    "action_key_hash": action_key,
                    "error_code": "multiple_action_key_matches",
                    "match_count": len(matches),
                },
            )
            return TaskReconciliationResult(status="multiple", matches=matches)

        expected_title = candidate.content.title.value
        if (
            len(matches) != 1
            or not isinstance(expected_title, str)
            or _normalized(matches[0].title) != _normalized(expected_title)
        ):
            error_code = (
                "task_reconciliation_not_found"
                if not matches
                else "task_reconciliation_title_mismatch"
            )
            _LOGGER.warning(
                "task reconciliation did not confirm a unique matching title",
                extra={
                    "event": "task_reconciliation_unknown",
                    "candidate_id": candidate_id,
                    "action_key_hash": action_key,
                    "error_code": error_code,
                    "match_count": len(matches),
                },
            )
            return TaskReconciliationResult(status="none", matches=())
        return TaskReconciliationResult(status="single", matches=matches)

    async def _prepare_candidate(
        self,
        candidate_id: str,
        *,
        expected_revision: int,
        actor_id: str,
        responsible_subject_required: bool,
    ) -> _PreparedCandidate:
        candidate = self._load_candidate(
            candidate_id,
            expected_revision=expected_revision,
        )
        if candidate.readiness not in {
            "recordable",
            "executable",
            "fully_specified",
        }:
            raise TaskNeedsInput(
                "candidate_not_recordable",
                "Please confirm the meeting action before creating a task.",
            )
        if candidate.content_status != "active":
            raise TaskNeedsInput(
                "candidate_not_active",
                "This meeting action is no longer active.",
            )
        if candidate.content.blocking_conflict_message_ids:
            raise TaskNeedsInput(
                "candidate_has_blocking_conflict",
                "Resolve the conflicting meeting evidence before creating a task.",
            )
        title = candidate.content.title.value
        deliverable = candidate.content.deliverable.value
        if (
            candidate.content.title.resolution != "known"
            or title is None
            or candidate.content.deliverable.resolution != "known"
            or deliverable is None
        ):
            raise TaskNeedsInput(
                "candidate_core_fields_unresolved",
                "Please confirm the task title and deliverable.",
            )
        if candidate.content.due_at.resolution in {"ambiguous", "conflicting"}:
            raise TaskNeedsInput(
                "candidate_due_unresolved",
                "Please confirm the requested due date.",
            )

        unresolved_identity: ResolvedIdentity | None = None
        assignee_ref: str | None = None
        assignee = candidate.content.assignee
        if assignee.value is not None:
            resolved = await self._identity_resolver.resolve(
                PersonMention(
                    spoken_text=assignee.value.spoken_text,
                    actor_id=actor_id,
                    explicit_external_user_id=assignee.value.linear_user_id,
                    email=(
                        assignee.value.spoken_text
                        if "@" in assignee.value.spoken_text
                        else None
                    ),
                )
            )
            if resolved.resolution == "known":
                assignee_ref = resolved.external_user_id
            else:
                unresolved_identity = resolved
        elif responsible_subject_required:
            raise TaskNeedsInput(
                "candidate_assignee_missing",
                "Please identify the responsible subject in the meeting text.",
            )

        evidence_ids = tuple(
            dict.fromkeys(
                evidence_id
                for grounded in (
                    candidate.content.title,
                    candidate.content.deliverable,
                    candidate.content.assignee,
                    candidate.content.due_at,
                    candidate.content.priority,
                )
                for evidence_id in grounded.evidence_message_ids
            )
        )
        available_evidence = {
            message.message_id for message in candidate.content.evidence_messages
        }
        if not evidence_ids or not set(evidence_ids).issubset(available_evidence):
            raise ValueError("Candidate evidence is incomplete")
        action_key = derive_task_create_key(
            self._adapter.connection.team_id,
            candidate.lineage_root_id,
        )
        description_lines = [
            deliverable,
            "",
            "Meeting evidence: " + ", ".join(evidence_ids),
            f"matinier-action-key: {action_key}",
        ]
        if unresolved_identity is not None:
            description_lines.extend(
                (
                    "",
                    f"Unresolved meeting assignee: {unresolved_identity.spoken_text}",
                    "matinier-identity-status: unresolved",
                )
            )
        due_at = (
            candidate.content.due_at.value
            if candidate.content.due_at.resolution == "known"
            else None
        )
        priority = (
            candidate.content.priority.value
            if candidate.content.priority.resolution == "known"
            else None
        )
        return _PreparedCandidate(
            candidate=candidate,
            action_key=action_key,
            unresolved_identity=unresolved_identity,
            draft=TaskDraft(
                title=title,
                description="\n".join(description_lines),
                assignee_ref=assignee_ref,
                due_at=due_at,
                priority=priority,
                source_evidence_ids=evidence_ids,
            ),
        )

    def _load_candidate(
        self,
        candidate_id: str,
        *,
        expected_revision: int | None,
    ) -> _CandidateSnapshot:
        with self._database.session() as db_session:
            record = db_session.get(ActionCandidateRecord, candidate_id)
            if record is None:
                raise LookupError(f"Action Candidate not found: {candidate_id}")
            revision_number = record.current_revision
            if (
                expected_revision is not None
                and revision_number != expected_revision
            ):
                raise ValueError("Action Candidate revision changed")
            revision = db_session.query(ActionCandidateRevisionRecord).filter_by(
                candidate_id=candidate_id,
                revision=revision_number,
            ).one()
            return _CandidateSnapshot(
                candidate_id=record.id,
                lineage_root_id=record.lineage_root_id,
                session_id=record.session_id,
                revision=revision.revision,
                readiness=revision.readiness,
                content_status=record.content_status,
                execution_status=record.execution_status,
                content=ActionCandidateContent.model_validate(
                    revision.content_json
                ),
            )

    @staticmethod
    def _classify(
        draft: TaskDraft,
        tasks: tuple[ExternalTask, ...],
        context: TaskDecisionContext,
        *,
        team_id: str,
    ) -> tuple[str, ExternalTask | None, tuple[str, ...]]:
        related: list[str] = []
        title_tokens = _tokens(draft.title)
        for task in tasks:
            assignee_compatible = (
                draft.assignee_ref is None
                or task.assignee_ref is None
                or draft.assignee_ref == task.assignee_ref
            )
            due_compatible = (
                draft.due_at is None
                or task.due_at is None
                or _same_due_date(draft.due_at, task.due_at)
            )
            exact_deliverable = _normalized(task.title) == _normalized(draft.title)
            agents_agree = (
                context.evidence_confirms_single_deliverable
                and context.conflict_agent_agrees
                and context.research_agent_agrees
            )
            if (
                task.status == "active"
                and task.team_id == team_id
                and exact_deliverable
                and assignee_compatible
                and due_compatible
                and agents_agree
            ):
                return "duplicate", task, ()
            overlap = title_tokens & _tokens(task.title)
            if overlap and (not assignee_compatible or not due_compatible):
                return "ambiguous", None, ()
            if overlap:
                related.append(task.external_id)
        if related:
            return "related", None, tuple(dict.fromkeys(related))
        return "distinct", None, ()

    @staticmethod
    def _verify_read_back(draft: TaskDraft, task: ExternalTask) -> None:
        if (
            task.title != draft.title
            or task.assignee_ref != draft.assignee_ref
            or (
                (task.due_at is None) != (draft.due_at is None)
                or (
                    task.due_at is not None
                    and draft.due_at is not None
                    and not _same_due_date(task.due_at, draft.due_at)
                )
            )
        ):
            raise RuntimeError("created task did not match its verified read-back")

    def _mark_candidate_executing(self, candidate_id: str) -> None:
        with self._database.session() as db_session:
            repository = MeetingStateRepository(db_session)
            record = db_session.get(ActionCandidateRecord, candidate_id)
            if record is None:
                raise LookupError(f"Action Candidate not found: {candidate_id}")
            if record.execution_status in {"not_requested", "handed_off"}:
                repository.transition_candidate_execution(candidate_id, "executing")
            db_session.commit()

    def _mark_candidate_executed(self, candidate_id: str) -> None:
        with self._database.session() as db_session:
            repository = MeetingStateRepository(db_session)
            record = db_session.get(ActionCandidateRecord, candidate_id)
            if record is None:
                raise LookupError(f"Action Candidate not found: {candidate_id}")
            if record.execution_status in {"not_requested", "handed_off"}:
                record = repository.transition_candidate_execution(
                    candidate_id,
                    "executing",
                )
            if record.execution_status == "executing":
                repository.transition_candidate_execution(candidate_id, "executed")
            db_session.commit()


__all__ = [
    "TaskNeedsInput",
    "TaskSystemService",
    "derive_task_create_key",
]
