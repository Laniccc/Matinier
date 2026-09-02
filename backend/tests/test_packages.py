from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.packages import (
    PackageBuilder,
    PackageRepository,
    PackageValidator,
    PackageZipExporter,
)
from app.packages.exporter import verify_package_zip
from app.packages.models import (
    EvidenceIndexDocument,
    LiveTranslationDocument,
    SourceRawDocument,
)
from app.persistence.database import Database
from app.persistence.models import (
    PackageDocumentRecord,
    SegmentRecord,
    SessionRecord,
    TranslationSegmentRecord,
)


NOW = dt.datetime(2026, 8, 4, 8, 0, tzinfo=dt.UTC)


def seed_completed_session(db_session: Session) -> str:
    session_id = "package-session"
    db_session.add(
        SessionRecord(
            id=session_id,
            room_name="package-room",
            status="completed",
            source_type="hls",
            source_name="https://example.com/live.m3u8?token=secret",
            language="zh-CN",
            target_language="en-US",
            translation_status="completed",
            asr_provider="bailian",
            asr_model="fun-asr-realtime",
            translation_provider="bailian",
            translation_model="live-translate",
            final_result_count=1,
            provider_error_count=0,
            sent_audio_chunk_count=10,
            sent_audio_bytes=6_400,
            created_at=NOW,
            started_at=NOW,
            ended_at=NOW + dt.timedelta(seconds=2),
        )
    )
    db_session.flush()
    db_session.add_all(
        [
            SegmentRecord(
                id="source-row-final",
                session_id=session_id,
                segment_id="source-final",
                track_id="track-1",
                revision=3,
                language="zh-CN",
                raw_text="原始文本",
                display_text="最终文本",
                audio_start_ms=100,
                audio_end_ms=1_100,
                confidence=0.95,
                status="final",
                received_at_ms=int(NOW.timestamp() * 1_000),
                finalized_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            ),
            SegmentRecord(
                id="source-row-draft",
                session_id=session_id,
                segment_id="source-draft",
                track_id="track-1",
                revision=1,
                language="zh-CN",
                raw_text="草稿不得进入成果包",
                display_text="草稿不得进入成果包",
                audio_start_ms=1_200,
                audio_end_ms=1_600,
                confidence=None,
                status="draft",
                received_at_ms=int(NOW.timestamp() * 1_000),
                finalized_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            ),
            TranslationSegmentRecord(
                id="translation-row-final",
                session_id=session_id,
                segment_id="translation-final",
                revision=2,
                source_language="zh-CN",
                target_language="en-US",
                text="Final text",
                audio_start_ms=100,
                audio_end_ms=1_100,
                source_segment_ids=["source-final"],
                status="final",
                received_at_ms=int(NOW.timestamp() * 1_000),
                finalized_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            ),
        ]
    )
    db_session.flush()
    return session_id


def test_builder_freezes_final_only_package_and_versions_without_overwrite() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            session_id = seed_completed_session(db_session)
            first = PackageBuilder(db_session).build_baseline(session_id)
            db_session.commit()

            assert PackageValidator().validate(first).valid is True
            source = next(
                item
                for item in first.documents
                if isinstance(item, SourceRawDocument)
            )
            translation = next(
                item
                for item in first.documents
                if isinstance(item, LiveTranslationDocument)
            )
            evidence = next(
                item
                for item in first.documents
                if isinstance(item, EvidenceIndexDocument)
            )
            assert [item.text for item in source.content.items] == ["最终文本"]
            assert translation.content.items[0].source_segment_ids == (
                "source-final",
            )
            assert evidence.content.items[0].item_id == source.content.items[0].item_id
            assert "token=secret" not in json.dumps(
                first.model_dump(mode="json"),
                ensure_ascii=False,
            )

            first_hash = first.content_hash
            first_document_hashes = tuple(
                item.content_hash for item in first.documents
            )
            second = PackageBuilder(db_session).build_baseline(session_id)
            db_session.commit()
            reloaded_first = PackageRepository(db_session).load(first.package_id)

            assert second.package_version == 2
            assert second.content_hash != first_hash
            assert reloaded_first.status == "superseded"
            assert reloaded_first.content_hash == first_hash
            assert tuple(
                item.content_hash for item in reloaded_first.documents
            ) == first_document_hashes
            first_source = next(
                item
                for item in reloaded_first.documents
                if isinstance(item, SourceRawDocument)
            )
            second_source = next(
                item
                for item in second.documents
                if isinstance(item, SourceRawDocument)
            )
            assert first_source.content.items[0].item_id == (
                second_source.content.items[0].item_id
            )
            assert db_session.get(SegmentRecord, "source-row-final").display_text == (
                "最终文本"
            )
    finally:
        database.dispose()


def test_validator_detects_document_tampering() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = PackageBuilder(db_session).build_baseline(
                seed_completed_session(db_session)
            )
            db_session.commit()
            source_record = next(
                item
                for item in PackageRepository(db_session).list_documents(
                    package.package_id
                )
                if item.document_kind == "source_raw"
            )
            changed = dict(source_record.content_json)
            changed["items"] = [
                {**changed["items"][0], "text": "被篡改的文本"}
            ]
            source_record.content_json = changed
            db_session.commit()

            result = PackageValidator().validate(
                PackageRepository(db_session).load(package.package_id)
            )
            assert result.valid is False
            assert "document hash mismatch: source_raw" in result.errors
    finally:
        database.dispose()


def test_package_zip_is_deterministic_and_checksums_cover_every_entry() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = PackageBuilder(db_session).build_baseline(
                seed_completed_session(db_session)
            )
            db_session.commit()
            exporter = PackageZipExporter()
            first = exporter.export(package)
            second = exporter.export(package)

            assert first == second
            valid, errors = verify_package_zip(first)
            assert valid is True
            assert errors == ()
    finally:
        database.dispose()


def test_package_document_identity_is_unique_without_language() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = PackageBuilder(db_session).build_baseline(
                seed_completed_session(db_session)
            )
            db_session.commit()
            original = next(
                item
                for item in PackageRepository(db_session).list_documents(
                    package.package_id
                )
                if item.document_kind == "timeline_index"
            )
            db_session.add(
                PackageDocumentRecord(
                    id="duplicate-timeline-document",
                    package_id=original.package_id,
                    document_kind=original.document_kind,
                    language=None,
                    content_json=original.content_json,
                    content_hash=original.content_hash,
                    created_at=original.created_at,
                )
            )

            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
    finally:
        database.dispose()
