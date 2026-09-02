import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from app.assistant.plugin_repository import MeetingPluginDenied
from app.persistence.models import ActionCandidateRecord, AssistantActionIntentRecord, ActionGrantRecord, MeetingPluginSessionRecord
from app.plugins.host_actions import HostActions
from app.plugins.host_action_contracts import HostActionPrepareInput, HostActionConfirmInput
from app.plugins.repository import PluginRepository
from app.settings import Settings
from meeting_plugin_fakes import install_meeting_scope

MEETING_PERMISSIONS = (
    "meeting.state.query", "meeting.operation.query", "meeting.turn.submit", "meeting.mark.write",
    "meeting.execution.submit", "meeting.execution.input", "meeting.execution.cancel",
)
ORIGIN = "http://127.0.0.1:3000"


def make_actions(f, **overrides):
    install_meeting_scope(f.database)
    with f.database.session() as db:
        PluginRepository(db).replace_base_permissions(plugin_id="com.matinier.meeting-assistant", version="1.0.0", permissions=MEETING_PERMISSIONS)
        # This action fixture represents a projected candidate, while the shared
        # legacy-history fixture deliberately has no projection head.
        from app.assistant.plugin_service import MeetingPluginReadService
        from app.meeting_state.models import MeetingState
        from app.persistence.models import MeetingStateHeadRecord
        from app.assistant.plugin_repository import canonical_hash
        if db.get(MeetingStateHeadRecord, f.session_id) is None:
            candidate = MeetingPluginReadService(db).require_candidate(f.session_id, f.candidate_id)
            state = MeetingState(session_id=f.session_id, version=1, action_candidates=(candidate,))
            db.add(MeetingStateHeadRecord(session_id=f.session_id, version=1, status="ready",
                state_json=state.model_dump(mode="json"), state_hash=canonical_hash(state.model_dump(mode="json")),
                source_frontier_json={"final-1": 2}, lag_ms=0, pending_segment_count=0))
        db.commit()
    settings = Settings(_env_file=None, **{"assistant_enabled": True, "plugin_framework_enabled": True,
        "task_system_provider": "fake", "linear_team_id": "team-1", **overrides})
    actions = HostActions(f.database, settings)
    nonce = actions.issue_ui_nonce(ORIGIN)
    return actions, actions.require_ui_context(nonce, ORIGIN)


def prepare(actions, context, f, *, action="meeting.execute", **changes):
    arguments = {"message": "Prepare release notes", "candidate_ids": [f.candidate_id]} if action == "meeting.execute" else {"message": "Explain"}
    return actions.prepare(context, f.media_id, HostActionPrepareInput.model_validate({
        "action": action, "request_id": "request-1", "plugin_version": "1.0.0",
        "arguments": arguments, **changes,
    }))


def confirm_input(preview, **changes):
    return HostActionConfirmInput(preview_id=preview["preview_id"], preview_hash=preview["preview_hash"], confirmed=True, **changes)


def test_prepare_does_not_grant_and_cancel_creates_nothing(meeting_history):
    f = meeting_history
    actions, context = make_actions(f)
    preview = prepare(actions, context, f)
    assert preview["confirmation_required"] and preview["team_id"] == "team-1"
    assert preview["max_side_effects"] == 1
    assert preview["candidates"][0]["candidate_id"] == f.candidate_id
    with f.database.session() as db:
        assert db.scalar(select(func.count(ActionGrantRecord.id))) == 0
        assert db.scalar(select(func.count(AssistantActionIntentRecord.id))) == 0
    cancelled = actions.confirm(context, f.media_id, HostActionConfirmInput(preview_id=preview["preview_id"], preview_hash=preview["preview_hash"], confirmed=False))
    assert cancelled == {"status": "cancelled"}
    with f.database.session() as db:
        assert db.scalar(select(func.count(AssistantActionIntentRecord.id))) == 0


def test_confirm_is_single_issuance_and_persists_no_replayable_token(meeting_history):
    f = meeting_history
    actions, context = make_actions(f)
    preview = prepare(actions, context, f)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: actions.confirm(context, f.media_id, confirm_input(preview)), range(2)))
    assert responses[0] == responses[1]
    token = responses[0]["command"]["intent_token"]
    with f.database.session() as db:
        rows = list(db.scalars(select(AssistantActionIntentRecord)))
        assert len(rows) == 1 and rows[0].token_hash != token
        assert token not in str(rows[0].scope_json)
        assert rows[0].scope_json["actor_id"] == "local-user"
        assert db.scalar(select(func.count(ActionGrantRecord.id))) == 0


def test_confirmed_deactivation_stops_analysis_without_revoking_interaction(meeting_history):
    f = meeting_history
    actions, context = make_actions(f)
    for action in ("meeting.analysis.activate", "meeting.analysis.deactivate"):
        preview = prepare(actions, context, f, action=action, arguments={}, request_id=action)
        actions.confirm(context, f.media_id, confirm_input(preview))
    with f.database.session() as db:
        row = db.get(MeetingPluginSessionRecord, f.session_id)
        assert row.analysis_state == "inactive" and row.analysis_epoch == 2
        assert row.authority_epoch == 0


@pytest.mark.parametrize("change", ["candidate", "team", "expired", "hash", "scope", "nonce"])
def test_confirmation_rejects_changed_scope(meeting_history, change):
    f = meeting_history
    actions, context = make_actions(f)
    preview = prepare(actions, context, f)
    payload = confirm_input(preview)
    if change == "candidate":
        with f.database.session() as db:
            db.get(ActionCandidateRecord, f.candidate_id).execution_status = "executed"
            db.commit()
    elif change == "team":
        actions.settings.linear_team_id = "other-team"
    elif change == "expired":
        actions.now = lambda: dt.datetime.now(dt.UTC) + dt.timedelta(minutes=16)
    elif change == "hash":
        payload = payload.model_copy(update={"preview_hash": "a" * 64})
    elif change == "nonce":
        context = actions.require_ui_context(actions.issue_ui_nonce(ORIGIN), ORIGIN)
    with pytest.raises(MeetingPluginDenied):
        actions.confirm(context, f.other_media_id if change == "scope" else f.media_id, payload)
    with f.database.session() as db:
        assert db.scalar(select(func.count(AssistantActionIntentRecord.id))) == 0


def test_activation_requires_host_confirmation_and_preserves_separate_epochs(meeting_history):
    f = meeting_history
    actions, context = make_actions(f)
    preview = prepare(actions, context, f, action="meeting.analysis.activate", arguments={})
    with f.database.session() as db:
        assert db.get(MeetingPluginSessionRecord, f.session_id).analysis_state == "inactive"
    result = actions.confirm(context, f.media_id, confirm_input(preview))
    assert result["status"] == "applied"
    with f.database.session() as db:
        row = db.get(MeetingPluginSessionRecord, f.session_id)
        assert (row.analysis_state, row.analysis_epoch, row.authority_epoch) == ("active", 1, 0)
