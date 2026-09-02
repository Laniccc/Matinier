from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from sqlalchemy import event, select

from app.assistant.plugin_policy import MeetingPluginPolicy
from app.assistant.plugin_repository import MeetingPluginDenied
from app.persistence.models import (
    MeetingPluginSessionRecord, PluginInstallationRecord, PluginPackageRecord,
    PluginPermissionRecord, PluginSessionBindingRecord, SessionRecord,
)
from meeting_plugin_fakes import install_meeting_scope


def test_default_deny_install_bind_is_not_activation(meeting_history):
    f = meeting_history
    policy = MeetingPluginPolicy(f.database)
    assert not policy.admission(f.session_id).allowed
    install_meeting_scope(f.database)
    assert not policy.admission(f.session_id).allowed
    grant = policy.activate(f.session_id, f.media_id, plugin_version="1.0.0", actor_id="local-user")
    assert grant.allowed and grant.analysis_epoch == 1
    assert not policy.admission(f.other_session_id).allowed
    assert policy.activate(f.session_id, f.media_id, plugin_version="1.0.0", actor_id="local-user").analysis_epoch == 1


def test_analysis_and_authority_epochs_are_independent(meeting_history):
    f = meeting_history
    install_meeting_scope(f.database)
    policy = MeetingPluginPolicy(f.database)
    first = policy.activate(f.session_id, f.media_id, plugin_version="1.0.0", actor_id="local-user")
    policy.deactivate(f.session_id)
    with f.database.session() as db:
        row = db.get(MeetingPluginSessionRecord, f.session_id)
        assert (row.analysis_epoch, row.authority_epoch) == (2, 0)
        with pytest.raises(MeetingPluginDenied):
            policy.fence(db, f.session_id, first.analysis_epoch)
    second = policy.activate(f.session_id, f.media_id, plugin_version="1.0.0", actor_id="local-user")
    assert second.analysis_epoch == 3
    policy.revoke(f.session_id)
    with f.database.session() as db:
        row = db.get(MeetingPluginSessionRecord, f.session_id)
        assert (row.analysis_state, row.analysis_epoch, row.authority_epoch) == ("inactive", 4, 1)


@pytest.mark.parametrize("failure", ["uninstalled", "disabled", "wrong_version", "ended", "host_disabled"])
def test_activation_rejects_ineligible_context(meeting_history, failure):
    f = meeting_history
    if failure != "uninstalled":
        install_meeting_scope(f.database)
    with f.database.session() as db:
        if failure == "disabled":
            db.scalar(select(PluginInstallationRecord)).status = "disabled"
        if failure == "ended":
            db.get(SessionRecord, f.session_id).status = "completed"
        db.commit()
    policy = MeetingPluginPolicy(f.database, enabled=failure != "host_disabled")
    with pytest.raises(MeetingPluginDenied):
        policy.activate(f.session_id, f.media_id, plugin_version="2.0.0" if failure == "wrong_version" else "1.0.0", actor_id="local-user")


@pytest.mark.parametrize("failure", ["disabled", "closed_binding", "permission", "signature", "version"])
def test_active_session_rechecks_current_installation_at_commit(meeting_history, failure):
    f = meeting_history
    install_meeting_scope(f.database)
    policy = MeetingPluginPolicy(f.database)
    grant = policy.activate(f.session_id, f.media_id, plugin_version="1.0.0", actor_id="local-user")
    with f.database.session() as db:
        if failure == "disabled":
            db.scalar(select(PluginInstallationRecord)).status = "disabled"
        elif failure == "closed_binding":
            db.scalar(select(PluginSessionBindingRecord)).status = "closed"
        elif failure == "permission":
            db.scalar(select(PluginPermissionRecord)).status = "revoked"
        elif failure == "signature":
            db.scalar(select(PluginPackageRecord)).signature_status = "invalid"
        elif failure == "version":
            db.scalar(select(PluginSessionBindingRecord)).plugin_version = "2.0.0"
        db.commit()
    assert not policy.admission(f.session_id).allowed
    with f.database.session() as db:
        with pytest.raises(MeetingPluginDenied):
            policy.fence(db, f.session_id, grant.analysis_epoch)


def test_concurrent_activation_is_idempotent(meeting_history):
    f = meeting_history
    install_meeting_scope(f.database)
    policy = MeetingPluginPolicy(f.database)
    barrier = Barrier(2)
    def activate():
        barrier.wait(timeout=3)
        return policy.activate(f.session_id, f.media_id, plugin_version="1.0.0", actor_id="local-user").analysis_epoch
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(activate) for _ in range(2)]
        assert [future.result(timeout=5) for future in futures] == [1, 1]


def test_commit_fence_serializes_with_concurrent_revocation(meeting_history):
    f = meeting_history
    install_meeting_scope(f.database)
    policy = MeetingPluginPolicy(f.database)
    grant = policy.activate(f.session_id, f.media_id, plugin_version="1.0.0", actor_id="local-user")
    revoke_attempted = Event()
    def before_execute(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE meeting_plugin_sessions") and "authority_epoch=" in statement:
            revoke_attempted.set()
    event.listen(f.database.engine, "before_cursor_execute", before_execute)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with f.database.session() as db:
                policy.fence(db, f.session_id, grant.analysis_epoch)
                pending = pool.submit(policy.revoke, f.session_id)
                assert revoke_attempted.wait(timeout=3)
                # The other writer has entered its SQL call but cannot finish
                # while this transaction holds the persistent admission fence.
                assert not pending.done()
                db.commit()
            pending.result(timeout=5)
    finally:
        event.remove(f.database.engine, "before_cursor_execute", before_execute)
    with f.database.session() as db:
        with pytest.raises(MeetingPluginDenied):
            policy.fence(db, f.session_id, grant.analysis_epoch)
