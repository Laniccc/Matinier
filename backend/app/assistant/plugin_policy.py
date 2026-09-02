"""Host-only meeting admission. A binding is never an analysis activation."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from app.assistant.plugin_repository import MEETING_PLUGIN_ID, MeetingPluginDenied, MeetingPluginRepository
from app.persistence.database import Database
from app.persistence.models import (
    MediaSessionRecord, MeetingPluginSessionRecord, MeetingProjectionOffsetRecord,
    PluginInstallationRecord, PluginPackageRecord, PluginPermissionRecord,
    PluginSessionBindingRecord, PluginRuntimeHealthRecord, SegmentRecord, SessionRecord, utc_now,
)


LIVE_SESSION_STATUSES = frozenset({"created", "starting", "active", "running"})
TERMINAL_SESSION_STATUSES = frozenset({"completed", "cancelled", "failed"})


@dataclass(frozen=True)
class AnalysisAdmission:
    allowed: bool = False
    analysis_epoch: int = -1
    authority_epoch: int = -1
    state: str = "inactive"
    terminal_frontier: Mapping[str, int] | None = None

    def permits(self, epoch: int) -> bool:
        return self.allowed and self.analysis_epoch == epoch

    def permits_segment(self, segment) -> bool:
        return self.allowed and (
            self.terminal_frontier is None
            or self.terminal_frontier.get(segment.segment_id) == segment.revision
        )


class MeetingPluginPolicy:
    def __init__(self, database: Database, *, enabled: bool = True, require_runtime_ready: bool = False):
        self._database = database
        self.enabled = enabled
        self.require_runtime_ready = require_runtime_ready

    @staticmethod
    def _bindings():
        permission = exists(select(PluginPermissionRecord.id).where(
            PluginPermissionRecord.package_id == PluginSessionBindingRecord.package_id,
            PluginPermissionRecord.plugin_id == MEETING_PLUGIN_ID,
            PluginPermissionRecord.plugin_version == PluginSessionBindingRecord.plugin_version,
            PluginPermissionRecord.permission_name == "meeting.state.query",
            PluginPermissionRecord.status == "accepted",
        ))
        return select(PluginSessionBindingRecord).join(
            PluginInstallationRecord,
            PluginInstallationRecord.preferred_package_id == PluginSessionBindingRecord.package_id,
        ).join(PluginPackageRecord, PluginPackageRecord.id == PluginSessionBindingRecord.package_id).where(
            PluginSessionBindingRecord.plugin_id == MEETING_PLUGIN_ID,
            PluginSessionBindingRecord.status == "open",
            PluginInstallationRecord.plugin_id == MEETING_PLUGIN_ID,
            PluginInstallationRecord.status == "enabled",
            PluginPackageRecord.plugin_id == MEETING_PLUGIN_ID,
            PluginPackageRecord.version == PluginSessionBindingRecord.plugin_version,
            PluginPackageRecord.signature_status == "verified", permission,
        )

    def eligible_session_ids(self):
        binding = self._bindings().with_only_columns(PluginSessionBindingRecord.id).where(
            PluginSessionBindingRecord.media_session_id == MeetingPluginSessionRecord.media_session_id,
            PluginSessionBindingRecord.plugin_version == MeetingPluginSessionRecord.plugin_version,
        ).exists()
        mapping = exists(select(MediaSessionRecord.id).where(
            MediaSessionRecord.id == MeetingPluginSessionRecord.media_session_id,
            MediaSessionRecord.legacy_session_id == MeetingPluginSessionRecord.legacy_session_id,
        ))
        readiness = exists(select(PluginRuntimeHealthRecord.id).where(
            PluginRuntimeHealthRecord.plugin_id == MEETING_PLUGIN_ID,
            PluginRuntimeHealthRecord.plugin_version == MeetingPluginSessionRecord.plugin_version,
            PluginRuntimeHealthRecord.status.in_(("ready", "degraded")),
        )) if self.require_runtime_ready else True
        return select(MeetingPluginSessionRecord.legacy_session_id).where(
            MeetingPluginSessionRecord.plugin_id == MEETING_PLUGIN_ID,
            MeetingPluginSessionRecord.analysis_state.in_(("active", "draining")),
            binding, mapping, readiness,
        )

    def admission(self, session_id: str, *, db: Session | None = None) -> AnalysisAdmission:
        if not self.enabled:
            return AnalysisAdmission()
        if db is None:
            with self._database.session() as session:
                return self.admission(session_id, db=session)
        row = db.get(MeetingPluginSessionRecord, session_id, populate_existing=True)
        if row is None:
            return AnalysisAdmission()
        qualified = db.scalar(self.eligible_session_ids().where(MeetingPluginSessionRecord.legacy_session_id == session_id)) is not None
        legacy = db.get(SessionRecord, session_id, populate_existing=True)
        if legacy is None or (legacy.status in TERMINAL_SESSION_STATUSES and row.analysis_state != "draining"):
            qualified = False
        if legacy is not None and legacy.status not in LIVE_SESSION_STATUSES | TERMINAL_SESSION_STATUSES | {"finalizing"}:
            qualified = False
        return AnalysisAdmission(
            allowed=qualified, analysis_epoch=row.analysis_epoch,
            authority_epoch=row.authority_epoch, state=row.analysis_state,
            terminal_frontier=MappingProxyType(dict(row.terminal_frontier_json)) if row.terminal_frontier_json is not None else None,
        )

    def activate(self, session_id: str, media_id: str, *, plugin_version: str, actor_id: str) -> AnalysisAdmission:
        """Trusted Host call, not a plugin capability; UI intent checked upstream."""
        if not self.enabled or not actor_id.strip() or len(actor_id) > 255:
            raise MeetingPluginDenied("meeting analysis is unavailable")
        with self._database.session() as db:
            legacy = db.get(SessionRecord, session_id)
            binding = db.scalar(self._bindings().where(
                PluginSessionBindingRecord.media_session_id == media_id,
                PluginSessionBindingRecord.plugin_version == plugin_version,
            ))
            if legacy is None or legacy.status not in LIVE_SESSION_STATUSES or binding is None:
                raise MeetingPluginDenied("analysis requires a live Session and enabled authorized binding")
            row = MeetingPluginRepository(db).ensure_session(legacy_session_id=session_id, media_session_id=media_id, plugin_version=plugin_version)
            if row.analysis_state != "active":
                row.analysis_state = "active"
                row.analysis_epoch += 1
                row.activated_by = actor_id
                row.activated_at = utc_now()
                row.stopped_reason = None
                row.terminal_frontier_json = None
            db.commit()
        return self.admission(session_id)

    def deactivate(self, session_id: str):
        with self._database.session() as db:
            MeetingPluginRepository(db).stop_analysis(session_id)
            db.commit()

    def revoke(self, session_id: str):
        with self._database.session() as db:
            MeetingPluginRepository(db).revoke_authority(session_id)
            db.commit()

    def fence(self, db: Session, session_id: str, epoch: int) -> AnalysisAdmission:
        """Acquire SQLite writer admission and retain it until caller commits.

        The conditional update serializes with stop/revoke, so a stale model
        result cannot pass a read-only check and then commit after revocation.
        """
        if not self.enabled:
            raise MeetingPluginDenied("meeting analysis is disabled")
        result = db.execute(update(MeetingPluginSessionRecord).where(
            MeetingPluginSessionRecord.legacy_session_id == session_id,
            MeetingPluginSessionRecord.analysis_epoch == epoch,
            MeetingPluginSessionRecord.legacy_session_id.in_(self.eligible_session_ids()),
        ).values(updated_at=MeetingPluginSessionRecord.updated_at).execution_options(synchronize_session=False))
        admission = self.admission(session_id, db=db)
        if result.rowcount != 1 or not admission.permits(epoch):
            raise MeetingPluginDenied("meeting analysis authority has changed")
        return admission

    def scan_admissions(self) -> dict[str, AnalysisAdmission]:
        if not self.enabled:
            return {}
        with self._database.session() as db:
            ids = list(db.scalars(self.eligible_session_ids()))
            for session_id in ids:
                row = db.get(MeetingPluginSessionRecord, session_id)
                legacy = db.get(SessionRecord, session_id)
                if row.analysis_state == "active" and legacy.status in TERMINAL_SESSION_STATUSES:
                    frontier = dict(db.execute(select(SegmentRecord.segment_id, SegmentRecord.revision).where(
                        SegmentRecord.session_id == session_id, SegmentRecord.status == "final",
                    )).all())
                    db.execute(update(MeetingPluginSessionRecord).where(
                        MeetingPluginSessionRecord.legacy_session_id == session_id,
                        MeetingPluginSessionRecord.analysis_epoch == row.analysis_epoch,
                        MeetingPluginSessionRecord.analysis_state == "active",
                    ).values(analysis_state="draining", terminal_frontier_json=frontier, updated_at=utc_now()).execution_options(synchronize_session=False))
            db.commit()
        admissions = {key: self.admission(key) for key in ids}
        for key, admission in tuple(admissions.items()):
            if admission.allowed and admission.state == "draining":
                self.finish_if_drained(key, admission.analysis_epoch)
                admissions[key] = self.admission(key)
        return {key: value for key, value in admissions.items() if value.allowed}

    def finish_if_drained(self, session_id: str, epoch: int):
        with self._database.session() as db:
            admission = self.admission(session_id, db=db)
            if not admission.permits(epoch) or admission.state != "draining":
                return
            offsets = dict(db.execute(select(MeetingProjectionOffsetRecord.segment_id, MeetingProjectionOffsetRecord.processed_revision).where(
                MeetingProjectionOffsetRecord.session_id == session_id,
            )).all())
            if any(offsets.get(key, 0) < revision for key, revision in (admission.terminal_frontier or {}).items()):
                return
            self.fence(db, session_id, epoch)
            db.execute(update(MeetingPluginSessionRecord).where(
                MeetingPluginSessionRecord.legacy_session_id == session_id,
                MeetingPluginSessionRecord.analysis_epoch == epoch,
            ).values(analysis_state="completed", updated_at=utc_now()).execution_options(synchronize_session=False))
            db.commit()
