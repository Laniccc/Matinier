from __future__ import annotations

from typing import Literal

from app.export import ExportDocument, export_markdown, export_srt, export_vtt
from app.packages.models import TranscriptPackage
from app.persistence.models import SessionRecord
from app.revisions.models import TranscriptRevision
from app.timeline.models import Timeline, TimelineSegment


RevisionExportFormat = Literal["srt", "vtt", "markdown"]


class RevisionExporter:
    def export(
        self,
        revision: TranscriptRevision,
        base_package: TranscriptPackage,
        export_format: RevisionExportFormat,
    ) -> bytes:
        if revision.base_package_id != base_package.package_id:
            raise ValueError("Revision does not belong to the supplied Package")
        snapshot = base_package.manifest.session_snapshot
        provider = base_package.manifest.provider_snapshot
        session = SessionRecord(
            id=revision.session_id,
            room_name=snapshot.room_name,
            status="completed",
            source_type=snapshot.source_type,
            source_name=f"Transcript revision v{revision.version}",
            language=revision.language,
            target_language=None,
            asr_provider=provider.asr_provider,
            asr_model=provider.asr_model,
            created_at=snapshot.created_at,
            started_at=snapshot.started_at,
            ended_at=snapshot.ended_at,
        )
        segments = tuple(
            TimelineSegment(
                id=item.item_id,
                session_id=revision.session_id,
                segment_id=item.item_id,
                track_id="transcript-revision",
                revision=revision.version,
                language=revision.language,
                raw_text=item.text,
                display_text=item.text,
                audio_start_ms=item.start_ms,
                audio_end_ms=item.end_ms,
                confidence=None,
                received_at_ms=int(revision.created_at.timestamp() * 1_000),
                finalized_at=revision.created_at,
                created_at=revision.created_at,
                updated_at=revision.created_at,
            )
            for item in revision.content.items
        )
        document = ExportDocument(
            session=session,
            timeline=Timeline(
                segments=segments,
                duration_ms=max((item.audio_end_ms for item in segments), default=0),
            ),
            exported_at=revision.created_at,
        )
        if export_format == "srt":
            return export_srt(document)
        if export_format == "vtt":
            return export_vtt(document)
        if export_format == "markdown":
            return export_markdown(document)
        raise ValueError(f"Unsupported Revision export format: {export_format}")
