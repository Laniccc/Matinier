"""Deterministic meeting fixtures; no network, production DB, or model calls."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from dataclasses import dataclass

from app.api.meeting_state import _manual_mark_message, _projection_segment
from app.assistant.repository import AssistantRepository
from app.meeting_state.candidates import caption_evidence_message
from app.meeting_state.contracts import MeetingStateDelta, StateItemProposal
from app.meeting_state.models import ActionCandidateContent, GroundedValue
from app.meeting_state.repository import MeetingStateRepository
from app.persistence.database import Database
from app.persistence.models import ExternalActionClaimRecord, MediaSessionRecord, SegmentRecord, SessionRecord
from app.task_system.fake import FakeTaskSystemAdapter


def final_segment(session_id="meeting-a", segment_id="final-1", *, revision=1, text="Prepare the release notes", at=None):
    at = at or dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
    return SegmentRecord(
        id=str(uuid.uuid4()), session_id=session_id, segment_id=segment_id,
        track_id="track-1", revision=revision, language="en", raw_text=text,
        display_text=text, audio_start_ms=1000, audio_end_ms=2000,
        confidence=0.99, status="final", received_at_ms=1000,
        finalized_at=at, created_at=at, updated_at=at,
    )


class ControlledMeetingModel:
    def __init__(self, *, blocked=False):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()

    async def extract(self, *, state, segments):
        self.calls.append(tuple((s.session_id, s.segment_id, s.revision) for s in segments))
        self.entered.set()
        await self.release.wait()
        return MeetingStateDelta(
            session_id=state.session_id,
            source_segment_ids=tuple(s.segment_id for s in segments),
            topics=tuple(StateItemProposal(text=s.display_text, source_segment_ids=(s.segment_id,)) for s in segments),
        )


class ControlledLinear(FakeTaskSystemAdapter):
    def __init__(self, *, lose_response=False):
        super().__init__()
        self.create_calls = self.read_calls = self.reconcile_calls = 0
        self.lose_response = lose_response

    async def create(self, draft, *, action_key):
        self.create_calls += 1
        result = await super().create(draft, action_key=action_key)
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError("fake response lost after remote commit")
        return result

    async def get(self, task_ref):
        self.read_calls += 1
        return await super().get(task_ref)

    async def reconcile_create(self, *, action_key):
        self.reconcile_calls += 1
        return await super().reconcile_create(action_key=action_key)


@dataclass(frozen=True)
class MeetingFixture:
    database: Database
    session_id: str = "meeting-a"
    other_session_id: str = "meeting-b"
    media_id: str = "media-a"
    other_media_id: str = "media-b"
    root_id: str = "meeting-root"
    child_id: str = "meeting-child"
    completed_id: str = "meeting-completed"
    candidate_id: str = "meeting-candidate"


def seed_meeting_history(database):
    fixture = MeetingFixture(database)
    now = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
    with database.session() as db:
        for legacy, media in ((fixture.session_id, fixture.media_id), (fixture.other_session_id, fixture.other_media_id)):
            db.add(SessionRecord(id=legacy, room_name=legacy, status="active", source_type="browser_tab", source_name="fixture", language="en"))
            db.flush()
            db.add(MediaSessionRecord(id=media, legacy_session_id=legacy, mode="live", source_kind="browser_tab", status="active"))
            db.add(final_segment(legacy, revision=2 if legacy == fixture.session_id else 1))
        db.flush()
        segment = db.query(SegmentRecord).filter_by(session_id=fixture.session_id).one()
        evidence = caption_evidence_message(_projection_segment(segment))
        meeting = MeetingStateRepository(db)
        for origin in ("manual", "automatic"):
            mark_id = f"{origin}-mark"
            messages = [evidence]
            if origin == "manual":
                messages.append(_manual_mark_message(mark_id=mark_id, session_id=fixture.session_id, actor_id="local-user", title="Important", note=None, created_at=now))
            meeting.create_mark(
                session_id=fixture.session_id, origin=origin, kind="highlight", title="Important",
                source_segment_ids=("final-1",), source_segment_revisions={"final-1": 2},
                evidence_messages=messages, source_state_version=0, mark_id=mark_id,
                audio_start_ms=1000, audio_end_ms=2000,
            )
        def known(value):
            return GroundedValue(value=value, origin="meeting_explicit", resolution="known", evidence_message_ids=(evidence.message_id,))
        def unknown():
            return GroundedValue(value=None, origin="server_default", resolution="missing")
        content = ActionCandidateContent(title=known("Release notes"), deliverable=known("Release notes"), assignee=unknown(), due_at=unknown(), priority=unknown(), evidence_messages=(evidence,))
        meeting.create_candidate(session_id=fixture.session_id, candidate_id=fixture.candidate_id, content=content, readiness="recordable")
        repo = AssistantRepository(db)
        root = repo.create_execution(session_id=fixture.session_id, profile="fast_turn", goal="Explain the release", execution_id=fixture.root_id, client_request_id="old-ask")
        for target in ("contextualizing", "deciding", "handed_off"):
            root = repo.transition_execution(root.id, expected_version=root.state_version, target_status=target, event_type="fixture.transition", summary=target)
        child = repo.create_execution(session_id=fixture.session_id, profile="action_run", goal="Prepare release notes", execution_id=fixture.child_id, parent_execution_id=root.id)
        for target in ("planning", "needs_input"):
            child = repo.transition_execution(child.id, expected_version=child.state_version, target_status=target, event_type="fixture.transition", summary=target, result={"question": "Which release?"})
        complete = repo.create_execution(session_id=fixture.session_id, profile="action_run", goal="Previous action", execution_id=fixture.completed_id)
        for target in ("planning", "executing", "observing", "completed"):
            complete = repo.transition_execution(complete.id, expected_version=complete.state_version, target_status=target, event_type="fixture.transition", summary=target, result={"external_reference": {"identifier": "FAKE-1", "url": "https://fake.tasks.local/1"}})
        db.add(ExternalActionClaimRecord(id="old-unknown-claim", provider="fake-task-system", capability="task.create", logical_action_key="old-action-key", holder_execution_id=child.id, status="unknown", arguments_hash="a" * 64, lease_expires_at=now, external_reference_json={"identifier": "FAKE-UNKNOWN"}))
        db.commit()
    return fixture


def install_meeting_scope(database, session_id="meeting-a", media_id="media-a"):
    """Seed a signed-package-shaped installation and accepted read permission."""
    from app.persistence.models import PluginPackageRecord
    from app.plugins.repository import PluginRepository
    plugin_id = "com.matinier.meeting-assistant"
    with database.session() as db:
        if db.get(MediaSessionRecord, media_id) is None:
            db.add(MediaSessionRecord(id=media_id, legacy_session_id=session_id, mode="live", source_kind="browser_tab", status="active"))
            db.flush()
        if db.get(PluginPackageRecord, "meeting-package") is None:
            db.add(PluginPackageRecord(id="meeting-package", plugin_id=plugin_id, version="1.0.0", content_digest="sha256:" + "d" * 64, manifest_hash="sha256:" + "e" * 64, image_digest="sha256:" + "f" * 64, signature_status="verified", package_path="fixture-only", manifest_json={}))
            db.flush()
        repo = PluginRepository(db)
        repo.ensure_installation(plugin_id=plugin_id, preferred_version="1.0.0", status="enabled")
        repo.replace_base_permissions(plugin_id=plugin_id, version="1.0.0", permissions=("meeting.state.query",))
        repo.bind_session(plugin_id=plugin_id, version="1.0.0", media_session_id=media_id, session_scope=f"scope-{media_id}")
        db.commit()


def authorize_test_meeting(database, session_id):
    from app.assistant.plugin_policy import MeetingPluginPolicy
    media_id = f"media-{session_id}"[:36]
    install_meeting_scope(database, session_id, media_id)
    policy = MeetingPluginPolicy(database)
    policy.activate(session_id, media_id, plugin_version="1.0.0", actor_id="fixture-user")
    return policy
