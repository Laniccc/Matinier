import datetime as dt

import pytest
from pydantic import ValidationError

from app.assistant.plugin_contracts import (
    MeetingAskInput, MeetingExecuteInput, MeetingMarkInput,
    MeetingInputInput, MeetingCancelInput, MeetingStateQueryInput,
    MeetingOperationQueryInput, MeetingOperationAccepted,
)
from app.plugins.host_action_contracts import HostActionIntent, HostActionScope


BASE = {"request_id": "request-1", "intent_token": "t" * 48}


@pytest.mark.parametrize("patch", [
    {"request_id": ""}, {"request_id": " "}, {"message": " "},
    {"message": "x" * 4001}, {"actor_id": "intruder"},
    {"session_id": "other"}, {"url": "https://example.org"},
    {"grant": {}}, {"mark_ids": ["same", "same"]},
])
def test_ask_rejects_invalid_or_privileged_fields(patch):
    with pytest.raises(ValidationError):
        MeetingAskInput.model_validate({**BASE, "message": "Question", **patch})


def test_execute_candidates_and_controls_are_bounded():
    for ids in ([], ["same", "same"], ["x"] * 21):
        with pytest.raises(ValidationError):
            MeetingExecuteInput.model_validate({**BASE, "message": "Create", "candidate_ids": ids})
    with pytest.raises(ValidationError):
        MeetingExecuteInput.model_validate({**BASE, "message": "Create", "candidate_ids": ["one"], "team_id": "override"})


@pytest.mark.parametrize("patch", [
    {"operation": "delete_all"}, {"operation": "accept"},
    {"operation": "create", "evidence": []},
    {"operation": "create", "evidence": [{"segment_id": "one", "revision": 0}]},
])
def test_mark_contract_rejects_unbounded_or_missing_evidence(patch):
    with pytest.raises(ValidationError):
        MeetingMarkInput.model_validate({**BASE, "title": "Mark", **patch})


def test_all_capability_contracts_have_valid_examples_and_forbid_extra():
    examples = [
        (MeetingAskInput, {**BASE, "message": "Question"}),
        (MeetingExecuteInput, {**BASE, "message": "Create", "candidate_ids": ["one"]}),
        (MeetingMarkInput, {**BASE, "operation": "create", "title": "Mark", "evidence": [{"segment_id": "one", "revision": 1}]}),
        (MeetingInputInput, {**BASE, "execution_id": "exec", "expected_state_version": 1, "input": "Answer"}),
        (MeetingCancelInput, {**BASE, "execution_id": "exec", "expected_state_version": 1}),
        (MeetingStateQueryInput, {"after": 0, "limit": 50}),
        (MeetingOperationQueryInput, {"operation_id": "op", "after": 0}),
        (MeetingOperationAccepted, {"operation_id": "op"}),
    ]
    for model, value in examples:
        assert model.model_validate(value)
        with pytest.raises(ValidationError):
            model.model_validate({**value, "unsafe": True})
    with pytest.raises(ValidationError):
        MeetingCancelInput.model_validate({**BASE, "execution_id": "exec", "expected_state_version": 0})


def test_host_intent_has_hash_only_and_bounded_aware_expiry():
    now = dt.datetime.now(dt.UTC)
    scope = HostActionScope(plugin_id="com.matinier.meeting-assistant", plugin_version="1.0.0", media_session_id="media-a", legacy_session_id="meeting-a", actor_id="local-user", authority_epoch=0, action="meeting.ask", payload_hash="a" * 64)
    intent = HostActionIntent(token_hash="b" * 64, scope=scope, created_at=now, expires_at=now + dt.timedelta(minutes=15))
    assert "intent_token" not in intent.model_dump()
    for expires in (now, now + dt.timedelta(minutes=16), now.replace(tzinfo=None)):
        with pytest.raises(ValidationError):
            HostActionIntent(token_hash="b" * 64, scope=scope, created_at=now, expires_at=expires)
