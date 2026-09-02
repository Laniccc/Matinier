from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.media.repository import MediaRepository
from app.packages import PackageBuilder
from app.packages.models import EvidenceIndexDocument
from app.persistence.database import Database
from app.persistence.models import MediaSessionRecord, SessionRecord
from app.plugins.broker import CapabilityExecutionContext
from app.plugins.document_contracts import (
    PluginDocumentEvidenceRef,
    PluginDocumentPublishInput,
)
from app.plugins.documents import PluginDocumentCapabilityAdapter
from app.plugins.repository import PluginRepository
from tests.test_packages import seed_completed_session


PLUGIN_ID = "com.matinier.course-organizer"
PLUGIN_VERSION = "1.0.0"


def seed_document_inputs(db_session: Session):
    legacy_session_id = seed_completed_session(db_session)
    media = MediaRepository(db_session).ensure_legacy_session_bridge(legacy_session_id)
    package = PackageBuilder(db_session).build_baseline(legacy_session_id)
    plugin_package = PluginRepository(db_session).record_package(
        plugin_id=PLUGIN_ID,
        version=PLUGIN_VERSION,
        content_digest="sha256:" + "1" * 64,
        manifest_hash="sha256:" + "2" * 64,
        image_digest="sha256:" + "3" * 64,
        signature_status="verified",
        package_path="plugins/packages/course-organizer",
        publisher_id=None,
        manifest_json={"id": PLUGIN_ID, "version": PLUGIN_VERSION},
    )
    evidence_document = next(
        item for item in package.documents if isinstance(item, EvidenceIndexDocument)
    )
    evidence = evidence_document.content.items[0]
    return media, package, plugin_package, evidence


def context(media_session_id: str) -> CapabilityExecutionContext:
    return CapabilityExecutionContext(
        plugin_id=PLUGIN_ID,
        plugin_version=PLUGIN_VERSION,
        generation=1,
        media_session_id=media_session_id,
        invocation_id="document-invocation-1",
    )


def publish_input(package_id: str, evidence, **updates) -> PluginDocumentPublishInput:
    payload = {
        "identity_key": "course-notes:zh-CN",
        "schema_name": "matinier.course-notes",
        "schema_version": "1.0",
        "language": "zh-CN",
        "trigger": "manual",
        "completeness": "interim",
        "source_package_id": package_id,
        "content": {"title": "课程内容整理", "sections": []},
        "markdown": "# 课程内容整理\n",
        "evidence_refs": [
            {
                "item_id": evidence.item_id,
                "source_segment_ids": list(evidence.source_segment_ids),
                "start_ms": evidence.start_ms,
                "end_ms": evidence.end_ms,
            }
        ],
    }
    payload.update(updates)
    return PluginDocumentPublishInput.model_validate(payload)


def test_document_capability_validates_evidence_and_versions_immutably() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with Session(database.engine) as db_session:
            media, package, _plugin_package, evidence = seed_document_inputs(db_session)
            adapter = PluginDocumentCapabilityAdapter(
                db_session,
                max_document_bytes=192 * 1024,
            )
            first_input = publish_input(package.package_id, evidence)
            first = asyncio.run(adapter(context(media.id), first_input))
            retried = asyncio.run(adapter(context(media.id), first_input))
            second = asyncio.run(
                adapter(
                    context(media.id),
                    publish_input(
                        package.package_id,
                        evidence,
                        content={
                            "title": "课程内容整理",
                            "sections": [{"id": "topic-1"}],
                        },
                    ),
                )
            )

            assert first.document_version == 1
            assert retried.document_id == first.document_id
            assert second.document_version == 2
            assert second.identity_key == "course-notes:zh-CN"
            assert second.language == "zh-CN"
            assert second.trigger == "manual"
            assert second.completeness == "interim"
            assert len(second.content_hash) == 64
    finally:
        database.dispose()


@pytest.mark.parametrize(
    "update",
    [
        {"evidence_refs": []},
        {
            "evidence_refs": [
                {
                    "item_id": "forged-item",
                    "source_segment_ids": ["source-final"],
                    "start_ms": 100,
                    "end_ms": 1_100,
                }
            ]
        },
        {
            "evidence_refs": [
                {
                    "item_id": "ITEM_FROM_PACKAGE",
                    "source_segment_ids": ["source-final"],
                    "start_ms": 101,
                    "end_ms": 1_100,
                }
            ]
        },
    ],
)
def test_document_capability_rejects_missing_forged_or_inexact_evidence(update) -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with Session(database.engine) as db_session:
            media, package, _plugin_package, evidence = seed_document_inputs(db_session)
            if update.get("evidence_refs"):
                for item in update["evidence_refs"]:
                    if item["item_id"] == "ITEM_FROM_PACKAGE":
                        item["item_id"] = evidence.item_id
            adapter = PluginDocumentCapabilityAdapter(
                db_session,
                max_document_bytes=192 * 1024,
            )
            with pytest.raises(ValueError, match="evidence"):
                asyncio.run(
                    adapter(
                        context(media.id),
                        publish_input(package.package_id, evidence, **update),
                    )
                )
    finally:
        database.dispose()


def test_document_capability_rejects_foreign_package_and_host_byte_overflow() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with Session(database.engine) as db_session:
            media, package, _plugin_package, evidence = seed_document_inputs(db_session)
            db_session.add(
                MediaSessionRecord(
                    id="foreign-media",
                    legacy_session_id=None,
                    mode="live",
                    source_kind="browser-tab",
                    status="active",
                    next_sequence=1,
                )
            )
            db_session.flush()
            adapter = PluginDocumentCapabilityAdapter(
                db_session,
                max_document_bytes=192 * 1024,
            )
            with pytest.raises(ValueError, match="current MediaSession"):
                asyncio.run(
                    adapter(
                        context("foreign-media"),
                        publish_input(package.package_id, evidence),
                    )
                )

            tiny = PluginDocumentCapabilityAdapter(db_session, max_document_bytes=200)
            with pytest.raises(ValueError, match="byte limit"):
                asyncio.run(
                    tiny(
                        context(media.id),
                        publish_input(package.package_id, evidence),
                    )
                )
    finally:
        database.dispose()


def test_document_contract_rejects_duplicate_reversed_and_unsafe_markdown() -> None:
    evidence = {
        "item_id": "item-1",
        "source_segment_ids": ["segment-1"],
        "start_ms": 100,
        "end_ms": 1_100,
    }
    base = {
        "identity_key": "course-notes:zh-CN",
        "schema_name": "matinier.course-notes",
        "schema_version": "1.0",
        "language": "zh-CN",
        "trigger": "manual",
        "completeness": "interim",
        "source_package_id": "package-1",
        "content": {"title": "Course"},
        "markdown": "# Course\n",
        "evidence_refs": [evidence],
    }
    with pytest.raises(ValidationError):
        PluginDocumentPublishInput.model_validate(
            {**base, "evidence_refs": [evidence, evidence]}
        )
    with pytest.raises(ValidationError):
        PluginDocumentPublishInput.model_validate(
            {
                **base,
                "evidence_refs": [{**evidence, "start_ms": 2_000, "end_ms": 1_000}],
            }
        )
    with pytest.raises(ValidationError):
        PluginDocumentPublishInput.model_validate(
            {**base, "markdown": "<script>unsafe</script>"}
        )
