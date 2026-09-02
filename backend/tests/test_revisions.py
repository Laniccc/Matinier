from __future__ import annotations

import datetime as dt
import uuid

import pytest
from pydantic import ValidationError

from app.packages import PackageBuilder, PackageRepository, PackageValidator
from app.packages.exporter import PackageZipExporter, verify_package_zip
from app.packages.models import SourceApprovedDocument, SourceRawDocument
from app.persistence.database import Database
from app.persistence.models import SegmentRecord
from app.revisions import (
    RevisionError,
    RevisionItem,
    RevisionRepository,
    RevisionService,
    TranscriptRevisionContent,
)
from tests.test_packages import NOW, seed_completed_session


def _add_second_final(db_session, session_id: str) -> None:
    db_session.add(
        SegmentRecord(
            id="source-row-final-2",
            session_id=session_id,
            segment_id="source-final-2",
            track_id="track-1",
            revision=2,
            language="zh-CN",
            raw_text="第二条原始文本",
            display_text="第二条最终文本",
            audio_start_ms=1_200,
            audio_end_ms=2_100,
            confidence=0.9,
            status="final",
            received_at_ms=int(NOW.timestamp() * 1_000),
            finalized_at=NOW,
            created_at=NOW + dt.timedelta(milliseconds=1),
            updated_at=NOW + dt.timedelta(milliseconds=1),
        )
    )
    db_session.flush()


def _seed_package(db_session):
    session_id = seed_completed_session(db_session)
    _add_second_final(db_session, session_id)
    package = PackageBuilder(db_session).build_baseline(session_id)
    return session_id, package


def test_revision_versions_merge_split_approval_and_stage1_immutability() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            session_id, package = _seed_package(db_session)
            original_final = [
                (item.segment_id, item.display_text)
                for item in db_session.query(SegmentRecord)
                .filter_by(session_id=session_id, status="final")
                .order_by(SegmentRecord.segment_id)
            ]
            service = RevisionService(db_session)
            first = service.create_from_package(package.package_id)
            assert first.version == 1
            assert len(first.content.items) == 2

            merged = service.create_version(
                first.revision_id,
                content=TranscriptRevisionContent(
                    items=(
                        RevisionItem(
                            item_id=str(uuid.uuid4()),
                            source_segment_ids=("source-final", "source-final-2"),
                            start_ms=100,
                            end_ms=2_100,
                            text="合并后的校对文本。",
                        ),
                    )
                ),
                change_summary="Merge adjacent captions",
            )
            split = service.create_version(
                merged.revision_id,
                content=TranscriptRevisionContent(
                    items=(
                        RevisionItem(
                            item_id=str(uuid.uuid4()),
                            source_segment_ids=("source-final", "source-final-2"),
                            start_ms=100,
                            end_ms=1_000,
                            text="拆分后的第一句。",
                        ),
                        RevisionItem(
                            item_id=str(uuid.uuid4()),
                            source_segment_ids=("source-final", "source-final-2"),
                            start_ms=1_000,
                            end_ms=2_100,
                            text="拆分后的第二句。",
                        ),
                    )
                ),
                change_summary="Split one caption",
            )
            assert (first.version, merged.version, split.version) == (1, 2, 3)
            assert merged.parent_revision_id == first.revision_id
            assert split.parent_revision_id == merged.revision_id

            service.approve(merged.revision_id)
            approved = service.approve(split.revision_id)
            assert approved.status == "approved"
            assert RevisionRepository(db_session).get(merged.revision_id).status == (
                "superseded"
            )
            current = [
                item
                for item in RevisionRepository(db_session).list_for_session(session_id)
                if item.status == "approved"
            ]
            assert [item.revision_id for item in current] == [split.revision_id]
            assert [
                (item.segment_id, item.display_text)
                for item in db_session.query(SegmentRecord)
                .filter_by(session_id=session_id, status="final")
                .order_by(SegmentRecord.segment_id)
            ] == original_final
    finally:
        database.dispose()


def test_approved_revision_builds_package_v2_without_mutating_v1() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            _, first_package = _seed_package(db_session)
            first_hash = first_package.content_hash
            first_document_hashes = tuple(
                item.content_hash for item in first_package.documents
            )
            service = RevisionService(db_session)
            first_revision = service.create_from_package(first_package.package_id)
            edited_items = list(first_revision.content.items)
            edited_items[0] = edited_items[0].model_copy(
                update={"text": "人工校对后的第一条。"}
            )
            edited = service.create_version(
                first_revision.revision_id,
                content=TranscriptRevisionContent(items=tuple(edited_items)),
                change_summary="Correct punctuation",
            )
            approved = service.approve(edited.revision_id)
            second_package = PackageBuilder(db_session).build_from_revision(approved)
            db_session.commit()

            reloaded_first = PackageRepository(db_session).load(
                first_package.package_id
            )
            assert reloaded_first.status == "superseded"
            assert reloaded_first.content_hash == first_hash
            assert tuple(
                item.content_hash for item in reloaded_first.documents
            ) == first_document_hashes
            assert second_package.package_version == 2
            assert second_package.source_revision_id == approved.revision_id
            approved_document = next(
                item
                for item in second_package.documents
                if isinstance(item, SourceApprovedDocument)
            )
            raw_document = next(
                item
                for item in second_package.documents
                if isinstance(item, SourceRawDocument)
            )
            assert second_package.manifest.effective_source_document_id == (
                approved_document.document_id
            )
            assert approved_document.content.items[0].text == (
                "人工校对后的第一条。"
            )
            assert raw_document.content.items[0].text == "最终文本"
            assert PackageValidator().validate(second_package).valid is True
            valid, errors = verify_package_zip(
                PackageZipExporter().export(second_package)
            )
            assert valid is True
            assert errors == ()
    finally:
        database.dispose()


def test_revision_rejects_lost_evidence_and_invalid_items() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            _, package = _seed_package(db_session)
            service = RevisionService(db_session)
            revision = service.create_from_package(package.package_id)
            with pytest.raises(RevisionError, match="dropped source segment"):
                service.create_version(
                    revision.revision_id,
                    content=TranscriptRevisionContent(
                        items=(revision.content.items[0],)
                    ),
                )
            with pytest.raises(ValidationError):
                RevisionItem(
                    item_id=str(uuid.uuid4()),
                    source_segment_ids=("source-final",),
                    start_ms=200,
                    end_ms=100,
                    text="invalid",
                )
            with pytest.raises(ValidationError):
                RevisionItem(
                    item_id=str(uuid.uuid4()),
                    source_segment_ids=("source-final",),
                    start_ms=0,
                    end_ms=100,
                    text="   ",
                )
            duplicate = revision.content.items[0]
            with pytest.raises(ValidationError, match="item IDs must be unique"):
                TranscriptRevisionContent(items=(duplicate, duplicate))
            with pytest.raises(ValidationError, match="must be a UUID"):
                RevisionItem(
                    item_id="not-a-uuid",
                    source_segment_ids=("source-final",),
                    start_ms=0,
                    end_ms=100,
                    text="invalid ID",
                )
    finally:
        database.dispose()
