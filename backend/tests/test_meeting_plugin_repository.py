import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select

from app.assistant.plugin_repository import MeetingPluginRepository, MeetingPluginConflict, MeetingPluginDenied
from app.plugins.host_action_contracts import HostActionIntent, HostActionScope
from app.persistence.models import MeetingPluginOperationRecord, AssistantActionIntentRecord


def scope_for(f, **changes):
    return HostActionScope.model_validate({"plugin_id": "com.matinier.meeting-assistant", "plugin_version": "1.0.0", "media_session_id": f.media_id, "legacy_session_id": f.session_id, "actor_id": "local-user", "authority_epoch": 0, "action": "meeting.ask", "payload_hash": "a" * 64, **changes})


def test_session_defaults_mapping_and_owner_are_checked(meeting_history):
    f = meeting_history
    with f.database.session() as db:
        repo = MeetingPluginRepository(db)
        row = repo.ensure_session(legacy_session_id=f.session_id, media_session_id=f.media_id, plugin_version="1.0.0")
        assert (row.analysis_state, row.analysis_epoch, row.authority_epoch) == ("inactive", 0, 0)
        assert repo.ensure_session(legacy_session_id=f.session_id, media_session_id=f.media_id, plugin_version="1.0.0").legacy_session_id == row.legacy_session_id
        with pytest.raises(MeetingPluginConflict):
            repo.ensure_session(legacy_session_id=f.session_id, media_session_id=f.other_media_id, plugin_version="1.0.0")
        with pytest.raises(MeetingPluginConflict):
            repo.ensure_session(legacy_session_id=f.session_id, media_session_id=f.media_id, plugin_version="2.0.0")


def test_operation_idempotency_and_rollback(meeting_history):
    f = meeting_history
    with f.database.session() as db:
        repo = MeetingPluginRepository(db)
        first = repo.accept_operation(scope=scope_for(f), client_request_id="one", request_payload={"message": "hello"})
        again = repo.accept_operation(scope=scope_for(f), client_request_id="one", request_payload={"message": "hello"})
        assert first.id == again.id
        with pytest.raises(MeetingPluginConflict):
            repo.accept_operation(scope=scope_for(f), client_request_id="one", request_payload={"message": "different"})
        db.rollback()
    with f.database.session() as db:
        assert db.scalar(select(func.count(MeetingPluginOperationRecord.id))) == 0


def test_intent_consumption_is_atomic_scoped_and_expiring(meeting_history):
    f = meeting_history
    now = dt.datetime.now(dt.UTC)
    scope = scope_for(f)
    with f.database.session() as db:
        repo = MeetingPluginRepository(db)
        intent = repo.create_intent(HostActionIntent(token_hash="b" * 64, scope=scope, created_at=now, expires_at=now + dt.timedelta(minutes=1)))
        db.commit()
        intent_id = intent.id
    barrier = Barrier(2)
    def consume():
        with f.database.session() as db:
            barrier.wait(timeout=3)
            try:
                MeetingPluginRepository(db).consume_intent(token_hash="b" * 64, expected_scope=scope, now=now)
                db.commit()
                return True
            except MeetingPluginDenied:
                db.rollback()
                return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: consume(), range(2)))
    assert sorted(outcomes) == [False, True]
    with f.database.session() as db:
        assert db.get(AssistantActionIntentRecord, intent_id).status == "consumed"
        repo = MeetingPluginRepository(db)
        for token, expected, at in (("c" * 64, scope, now + dt.timedelta(minutes=2)), ("d" * 64, scope_for(f, authority_epoch=1), now)):
            repo.create_intent(HostActionIntent(token_hash=token, scope=scope, created_at=now, expires_at=now + dt.timedelta(minutes=1)))
            with pytest.raises(MeetingPluginDenied):
                repo.consume_intent(token_hash=token, expected_scope=expected, now=at)


def test_analysis_stop_preserves_intents_and_revoke_is_plugin_scoped(meeting_history):
    f = meeting_history
    now = dt.datetime.now(dt.UTC)
    with f.database.session() as db:
        repo = MeetingPluginRepository(db)
        repo.ensure_session(legacy_session_id=f.session_id, media_session_id=f.media_id, plugin_version="1.0.0")
        meeting = repo.create_intent(HostActionIntent(token_hash="e" * 64, scope=scope_for(f), created_at=now, expires_at=now + dt.timedelta(minutes=1)))
        other = repo.create_intent(HostActionIntent(token_hash="f" * 64, scope=scope_for(f, plugin_id="com.example.other-plugin"), created_at=now, expires_at=now + dt.timedelta(minutes=1)))
        repo.stop_analysis(f.session_id)
        db.refresh(meeting)
        assert meeting.status == "active"
        repo.revoke_authority(f.session_id)
        db.refresh(meeting)
        db.refresh(other)
        assert meeting.status == "revoked"
        assert other.status == "active"
