from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.media.repository import MediaRepository
from app.packages import PackageBuilder
from app.persistence.database import Database
from app.persistence.models import PluginDocumentRecord
from app.plugins.document_contracts import (
    PluginDocumentEvidenceRef,
    PluginDocumentPublishInput,
)
from app.plugins.document_repository import PluginDocumentRepository
from app.plugins.repository import PluginRepository
from tests.test_packages import seed_completed_session


PLUGIN_ID = "com.matinier.course-organizer"
PLUGIN_VERSION = "1.0.0"


def _seed(database: Database):
    db_session = Session(database.engine)
    legacy_session_id = seed_completed_session(db_session)
    media = MediaRepository(db_session).ensure_legacy_session_bridge(legacy_session_id)
    package = PackageBuilder(db_session).build_baseline(legacy_session_id)
    plugin_package = PluginRepository(db_session).record_package(
        plugin_id=PLUGIN_ID,
        version=PLUGIN_VERSION,
        content_digest="sha256:" + "a" * 64,
        manifest_hash="sha256:" + "b" * 64,
        image_digest="sha256:" + "c" * 64,
        signature_status="verified",
        package_path="plugins/packages/course-organizer",
        publisher_id=None,
        manifest_json={"id": PLUGIN_ID, "version": PLUGIN_VERSION},
    )
    db_session.commit()
    return db_session, media, package, plugin_package


def _document(
    package_id: str,
    *,
    identity_key: str = "course-notes:zh-CN",
    language: str = "zh-CN",
    trigger: str = "manual",
    completeness: str = "interim",
    content: dict[str, object] | None = None,
    markdown: str = "# 课程内容整理\n",
    start_ms: int = 100,
) -> PluginDocumentPublishInput:
    return PluginDocumentPublishInput(
        identity_key=identity_key,
        schema_name="matinier.course-notes",
        schema_version="1.0",
        language=language,
        trigger=trigger,
        completeness=completeness,
        source_package_id=package_id,
        content=content or {"title": "课程内容整理", "sections": []},
        markdown=markdown,
        evidence_refs=(
            PluginDocumentEvidenceRef(
                item_id="knowledge-1",
                source_segment_ids=("source-final",),
                start_ms=start_ms,
                end_ms=1_100,
            ),
        ),
    )


def _publish(repository, plugin_package, media, package, value):
    return repository.publish(
        plugin_id=PLUGIN_ID,
        plugin_version=PLUGIN_VERSION,
        plugin_package_id=plugin_package.id,
        media_session_id=media.id,
        source_package_id=package.package_id,
        value=value,
    )


def test_publish_versions_are_immutable_language_scoped_and_idempotent() -> None:
    database = Database("sqlite://")
    database.create_schema()
    db_session, media, package, plugin_package = _seed(database)
    try:
        repository = PluginDocumentRepository(db_session)
        first_input = _document(package.package_id)
        first = _publish(
            repository, plugin_package, media, package, first_input
        )
        exact_retry = _publish(
            repository, plugin_package, media, package, first_input
        )
        second = _publish(
            repository,
            plugin_package,
            media,
            package,
            _document(
                package.package_id,
                content={"title": "课程内容整理", "sections": [{"id": "topic-1"}]},
            ),
        )
        english = _publish(
            repository,
            plugin_package,
            media,
            package,
            _document(
                package.package_id,
                identity_key="course-notes:en-US",
                language="en-US",
                content={"title": "Course notes", "sections": []},
                markdown="# Course notes\n",
            ),
        )
        db_session.commit()

        assert first.document_version == 1
        assert exact_retry.id == first.id
        assert exact_retry.document_version == 1
        assert second.document_version == 2
        assert english.document_version == 1
        assert first.content_json == {"title": "课程内容整理", "sections": []}
        assert repository.list_for_session(media.id, plugin_id=PLUGIN_ID) == [
            english,
            second,
            first,
        ]
    finally:
        db_session.close()
        database.dispose()


def test_document_foreign_keys_preserve_user_data_and_protect_source_package() -> None:
    database = Database("sqlite://")
    database.create_schema()
    db_session, media, package, plugin_package = _seed(database)
    try:
        repository = PluginDocumentRepository(db_session)
        published = _publish(
            repository,
            plugin_package,
            media,
            package,
            _document(package.package_id),
        )
        db_session.commit()

        db_session.delete(plugin_package)
        db_session.commit()
        db_session.expire_all()
        assert repository.get(published.id).plugin_package_id is None

        source_package = repository.require_source_package(package.package_id)
        db_session.delete(source_package)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

        media_record = repository.require_media_session(media.id)
        db_session.delete(media_record)
        db_session.commit()
        assert db_session.scalar(
            select(func.count()).select_from(PluginDocumentRecord)
        ) == 0
    finally:
        db_session.close()
        database.dispose()


def test_content_hash_covers_every_user_and_source_semantic() -> None:
    database = Database("sqlite://")
    database.create_schema()
    db_session, media, package, plugin_package = _seed(database)
    try:
        repository = PluginDocumentRepository(db_session)
        base = _document(package.package_id)
        hashes = {
            _publish(repository, plugin_package, media, package, base).content_hash
        }
        variants = (
            base.model_copy(update={"content": {"title": "changed"}}),
            base.model_copy(update={"markdown": "# Changed\n"}),
            base.model_copy(
                update={
                    "evidence_refs": (
                        base.evidence_refs[0].model_copy(update={"start_ms": 101}),
                    )
                }
            ),
            base.model_copy(update={"language": "en-US"}),
            base.model_copy(update={"trigger": "session_completed"}),
        )
        for value in variants:
            hashes.add(
                _publish(repository, plugin_package, media, package, value).content_hash
            )

        second_package = PackageBuilder(db_session).build_baseline(
            media.legacy_session_id
        )
        package_variant = base.model_copy(
            update={"source_package_id": second_package.package_id}
        )
        hashes.add(
            _publish(
                repository,
                plugin_package,
                media,
                second_package,
                package_variant,
            ).content_hash
        )
        assert len(hashes) == 7
    finally:
        db_session.close()
        database.dispose()


def test_publish_requires_existing_and_matching_persistence_owners() -> None:
    database = Database("sqlite://")
    database.create_schema()
    db_session, media, package, plugin_package = _seed(database)
    try:
        repository = PluginDocumentRepository(db_session)
        value = _document(package.package_id)
        with pytest.raises(LookupError, match="plugin package"):
            repository.publish(
                plugin_id=PLUGIN_ID,
                plugin_version=PLUGIN_VERSION,
                plugin_package_id="missing",
                media_session_id=media.id,
                source_package_id=package.package_id,
                value=value,
            )
        with pytest.raises(LookupError, match="MediaSession"):
            repository.publish(
                plugin_id=PLUGIN_ID,
                plugin_version=PLUGIN_VERSION,
                plugin_package_id=plugin_package.id,
                media_session_id="missing",
                source_package_id=package.package_id,
                value=value,
            )
        with pytest.raises(LookupError, match="source Package"):
            repository.publish(
                plugin_id=PLUGIN_ID,
                plugin_version=PLUGIN_VERSION,
                plugin_package_id=plugin_package.id,
                media_session_id=media.id,
                source_package_id="missing",
                value=value,
            )
    finally:
        db_session.close()
        database.dispose()
