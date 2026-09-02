"""Durable ownership checks shared by model checkpoints, recovery and final writes."""
from sqlalchemy import select

from app.assistant.plugin_repository import MeetingPluginDenied
from app.assistant.plugin_service import MeetingPluginReadService
from app.assistant.repository import AssistantRepository
from app.assistant.state_machine import is_terminal_execution_status
from app.persistence.models import ActionGrantRecord, AssistantExecutionRecord, MeetingPluginOperationRecord
from app.plugins.host_actions import begin_write


class MeetingExecutionGuard:
    def __init__(self, operations):
        self.operations = operations
        self.database = operations.database

    def ownership(self, db, execution):
        rows = list(db.scalars(select(MeetingPluginOperationRecord).where(
            MeetingPluginOperationRecord.execution_id.in_((execution.id, execution.root_execution_id)),
            MeetingPluginOperationRecord.action.in_(("meeting.ask", "meeting.execute")),
        ).order_by(MeetingPluginOperationRecord.created_at)))
        if not rows:
            raise MeetingPluginDenied("execution has no confirmed plugin ownership")
        return rows[0]

    def check_db(self, db, execution_id, *, allow_unowned_read=False):
        execution = db.get(AssistantExecutionRecord, execution_id, populate_existing=True)
        if execution is None or is_terminal_execution_status(execution.profile, execution.status):
            raise MeetingPluginDenied("execution is no longer active")
        try:
            owner = self.ownership(db, execution)
        except MeetingPluginDenied:
            if allow_unowned_read and execution.profile == "fast_turn":
                return None
            raise
        scope = self.operations.require_authority(db, owner)
        if execution.session_id != scope.legacy_session_id:
            raise MeetingPluginDenied("execution belongs to another meeting")
        # A submitted clarification is itself a fresh confirmed operation. It
        # cannot replace or broaden the original external-write authorization.
        for continuation in db.scalars(select(MeetingPluginOperationRecord).where(
                MeetingPluginOperationRecord.execution_id == execution.id,
                MeetingPluginOperationRecord.action == "meeting.input")):
            self.operations.require_authority(db, continuation)
        return scope

    def check(self, execution_id, *, allow_unowned_read=False):
        with self.database.session() as db:
            return self.check_db(db, execution_id, allow_unowned_read=allow_unowned_read)

    def fence(self, db, execution_id, *, allow_unowned_read=False):
        # Same SQLite writer boundary as revoke/disable. The caller retains it
        # through requesting + budget consumption, and releases before network.
        begin_write(db)
        return self.check_db(db, execution_id, allow_unowned_read=allow_unowned_read)

    def authorize_tool(self, db, request, spec):
        scope = self.fence(db, request.execution_id, allow_unowned_read=spec.effect != "external_write")
        if spec.effect != "external_write":
            return
        if scope is None or scope.action != "meeting.execute":
            raise MeetingPluginDenied("no confirmed external-write action")
        execution = db.get(AssistantExecutionRecord, request.execution_id)
        owner = self.ownership(db, execution)
        original = db.get(AssistantExecutionRecord, owner.execution_id)
        grant = db.get(ActionGrantRecord, execution.grant_id) if execution.grant_id else None
        team = self.operations.settings.linear_team_id or (
            "local-demo-team" if self.operations.settings.task_system_provider == "fake" else None)
        if (grant is None or execution.grant_id != original.grant_id
                or (request.grant_id is not None and request.grant_id != execution.grant_id)
                or grant.actor_id != scope.actor_id or grant.session_id != scope.legacy_session_id
                or grant.linear_team_id != scope.team_id or team != scope.team_id
                or grant.resource_scope_json != {"linear_team_id": scope.team_id}
                or set(grant.candidate_ids_json) != set(scope.candidate_revisions)
                or grant.max_side_effects != scope.max_side_effects
                or spec.capability != "task.create" or request.candidate_id not in scope.candidate_revisions):
            raise MeetingPluginDenied("external-write grant differs from user confirmation")
        candidate = MeetingPluginReadService(db).require_candidate(scope.legacy_session_id, request.candidate_id)
        if (candidate.current_revision != scope.candidate_revisions[request.candidate_id]
                or candidate.content_status != "active" or not candidate.evidence_revisions_current):
            raise MeetingPluginDenied("confirmed candidate or evidence changed")

    async def reconcile_read_only(self, execution_id, executor):
        """Isolate first; then use only existing tool-call reconciliation adapters."""
        from app.assistant.recovery import ActionRunRecovery
        with self.database.session() as db:
            begin_write(db)
            repo = AssistantRepository(db)
            execution = repo.get_execution_required(execution_id)
            if (not is_terminal_execution_status(execution.profile, execution.status)
                    and execution.status != "needs_input"):
                ActionRunRecovery._move_to_needs_input(repo, execution,
                    error_code="meeting_authority_required",
                    question="This execution requires renewed user confirmation; existing effects are only reconciled.")
            calls = repo.list_tool_calls(execution_id,
                statuses=("requesting", "pending", "unknown", "reconciling"), effect="external_write")
            ids = [call.id for call in calls]
            for call in calls:
                if call.status == "requesting":
                    repo.update_tool_call(call.id, expected_status="requesting", status="unknown",
                        error_code="interrupted_external_request")
            db.commit()
        if executor is not None:
            for identifier in ids:
                try:
                    await executor.reconcile(identifier)
                except (LookupError, ValueError, RuntimeError):
                    # Missing/changed adapters cannot authorize a retry. Retain
                    # the original unknown claim and public history for review.
                    continue
        return bool(ids)
