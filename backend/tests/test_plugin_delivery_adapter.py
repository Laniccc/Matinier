from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy.orm import Session

from app.media.repository import MediaRepository
from app.persistence.database import Database
from app.persistence.models import SessionRecord
from app.plugins.broker import CapabilityExecutionContext
from app.plugins.capabilities import DeliveryPrepareInput, DeliveryQueryInput
from app.plugins.delivery import DeliveryCapabilityAdapter
from tests.test_packages import seed_completed_session


ALL_KINDS = (
    "source_raw",
    "live_translation",
    "timeline_index",
    "evidence_index",
    "session_metadata",
    "provider_metadata",
    "metrics_metadata",
)


def _context(media_session_id: str) -> CapabilityExecutionContext:
    return CapabilityExecutionContext(
        plugin_id="com.matinier.course-organizer",
        plugin_version="1.0.0",
        generation=1,
        media_session_id=media_session_id,
        invocation_id="invocation-1",
    )


def _prepare(
    adapter: DeliveryCapabilityAdapter,
    media_session_id: str,
    *,
    trigger: str = "manual",
    final_sequence: int = 9,
):
    return asyncio.run(
        adapter.prepare(
            _context(media_session_id),
            DeliveryPrepareInput(
                trigger=trigger,
                output_language="zh-CN",
                final_sequence=final_sequence,
            ),
        )
    )


def _query(
    adapter: DeliveryCapabilityAdapter,
    media_session_id: str,
    package_id: str,
    *,
    kinds: tuple[str, ...] = ALL_KINDS,
    language: str | None = None,
    after_item: int = 0,
    limit: int = 100,
):
    return asyncio.run(
        adapter.query(
            _context(media_session_id),
            DeliveryQueryInput(
                package_id=package_id,
                document_kinds=kinds,
                language=language,
                after_item=after_item,
                limit=limit,
            ),
        )
    )


def test_prepare_builds_frozen_interim_and_terminal_packages() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with Session(database.engine) as db_session:
            legacy_session_id = seed_completed_session(db_session)
            legacy = db_session.get(SessionRecord, legacy_session_id)
            legacy.status = "running"
            legacy.ended_at = None
            media = MediaRepository(db_session).ensure_legacy_session_bridge(
                legacy_session_id
            )
            adapter = DeliveryCapabilityAdapter(db_session, max_page_items=100)

            interim = _prepare(adapter, media.id)
            assert interim.package_version == 1
            assert interim.final_sequence == 9
            assert len(interim.content_hash) == 64
            interim_metadata = _query(
                adapter,
                media.id,
                interim.package_id,
                kinds=("session_metadata",),
            )
            assert interim_metadata.items[0]["content"]["status"] == "running"

            legacy.status = "completed"
            terminal = _prepare(
                adapter,
                media.id,
                trigger="session_completed",
                final_sequence=10,
            )
            assert terminal.package_version == 2
            terminal_metadata = _query(
                adapter,
                media.id,
                terminal.package_id,
                kinds=("session_metadata",),
            )
            assert terminal_metadata.items[0]["content"]["status"] == "completed"
    finally:
        database.dispose()


def test_prepare_rejects_missing_bridge_and_session_without_finals() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with Session(database.engine) as db_session:
            db_session.add(
                SessionRecord(
                    id="silent-session",
                    room_name="silent-room",
                    status="running",
                    source_type="browser-tab",
                    source_name="Shared course tab",
                    language="en-US",
                )
            )
            db_session.flush()
            media = MediaRepository(db_session).ensure_legacy_session_bridge(
                "silent-session"
            )
            adapter = DeliveryCapabilityAdapter(db_session, max_page_items=100)
            with pytest.raises(ValueError, match="Session has no Final captions"):
                _prepare(adapter, media.id)
            with pytest.raises(ValueError, match="session-bound"):
                asyncio.run(
                    adapter.prepare(
                        _context("").__class__(
                            plugin_id="com.matinier.course-organizer",
                            plugin_version="1.0.0",
                            generation=1,
                            media_session_id=None,
                            invocation_id="invocation-2",
                        ),
                        DeliveryPrepareInput(
                            trigger="manual",
                            output_language="zh-CN",
                            final_sequence=0,
                        ),
                    )
                )
    finally:
        database.dispose()


def test_query_is_session_scoped_language_aware_and_deterministic() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with Session(database.engine) as db_session:
            legacy_session_id = seed_completed_session(db_session)
            media = MediaRepository(db_session).ensure_legacy_session_bridge(
                legacy_session_id
            )
            db_session.add(
                SessionRecord(
                    id="other-session",
                    room_name="other-room",
                    status="running",
                    source_type="browser-tab",
                    source_name="Other tab",
                    language="en-US",
                )
            )
            db_session.flush()
            other_media = MediaRepository(db_session).ensure_legacy_session_bridge(
                "other-session"
            )
            adapter = DeliveryCapabilityAdapter(db_session, max_page_items=100)
            prepared = _prepare(adapter, media.id)

            first = _query(adapter, media.id, prepared.package_id)
            repeated = _query(adapter, media.id, prepared.package_id)
            assert repeated == first
            kinds = {item["document_kind"] for item in first.items}
            assert kinds == set(ALL_KINDS)
            assert all(
                len(json.dumps(item, ensure_ascii=False).encode("utf-8")) < 192 * 1024
                for item in first.items
            )

            translated = _query(
                adapter,
                media.id,
                prepared.package_id,
                language="en-US",
            )
            assert any(
                item["document_kind"] == "live_translation"
                for item in translated.items
            )
            source_language = _query(
                adapter,
                media.id,
                prepared.package_id,
                language="zh-CN",
            )
            source_kinds = {item["document_kind"] for item in source_language.items}
            assert "live_translation" not in source_kinds
            assert {"source_raw", "evidence_index", "session_metadata"}.issubset(
                source_kinds
            )

            with pytest.raises(ValueError, match="current MediaSession"):
                _query(adapter, other_media.id, prepared.package_id)
    finally:
        database.dispose()


def test_query_pagination_has_no_gaps_duplicates_or_host_limit_escape() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with Session(database.engine) as db_session:
            legacy_session_id = seed_completed_session(db_session)
            media = MediaRepository(db_session).ensure_legacy_session_bridge(
                legacy_session_id
            )
            adapter = DeliveryCapabilityAdapter(db_session, max_page_items=2)
            prepared = _prepare(adapter, media.id)
            unpaged = DeliveryCapabilityAdapter(db_session, max_page_items=100)
            expected = _query(unpaged, media.id, prepared.package_id).items

            items: list[dict[str, object]] = []
            cursor = 0
            while True:
                page = _query(
                    adapter,
                    media.id,
                    prepared.package_id,
                    after_item=cursor,
                    limit=100,
                )
                assert len(page.items) <= 2
                items.extend(page.items)
                if page.next_after_item is None:
                    break
                assert page.next_after_item > cursor
                cursor = page.next_after_item

            assert tuple(items) == expected
            stable_keys = [
                (item["document_id"], item.get("item_id")) for item in items
            ]
            assert len(stable_keys) == len(set(stable_keys))
    finally:
        database.dispose()
