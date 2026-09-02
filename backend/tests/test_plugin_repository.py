from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.media.repository import MediaRepository
from app.persistence.database import Database
from app.persistence.models import (
    PluginAuditEventRecord,
    PluginCapabilityInvocationRecord,
    PluginPackageRecord,
    PluginPermissionRecord,
    SessionRecord,
)
from app.plugins.repository import (
    PluginRepository,
    PluginStateConflictError,
    PluginStateQuotaError,
    StagingTicketError,
)


@pytest.fixture
def database() -> Database:
    value = Database("sqlite://")
    value.create_schema()
    try:
        yield value
    finally:
        value.dispose()


def seed_media_session(db_session: Session) -> str:
    db_session.add(
        SessionRecord(
            id="legacy-1",
            room_name="room-1",
            status="running",
            source_type="browser-tab",
            source_name="Shared tab",
            language="en-US",
        )
    )
    db_session.flush()
    media = MediaRepository(db_session).ensure_legacy_session_bridge("legacy-1")
    return media.id


def install_versions(repository: PluginRepository) -> tuple[PluginPackageRecord, PluginPackageRecord]:
    publisher = repository.trust_publisher(
        name="Example Publisher",
        key_fingerprint="sha256:" + "1" * 64,
        public_key="ed25519-public-key",
    )
    first = repository.record_package(
        plugin_id="com.example.diagnostic",
        version="1.0.0",
        content_digest="sha256:" + "a" * 64,
        manifest_hash="sha256:" + "b" * 64,
        image_digest="sha256:" + "c" * 64,
        signature_status="verified",
        package_path="plugins/packages/v1",
        publisher_id=publisher.id,
        manifest_json={"id": "com.example.diagnostic", "version": "1.0.0"},
    )
    second = repository.record_package(
        plugin_id="com.example.diagnostic",
        version="1.1.0",
        content_digest="sha256:" + "d" * 64,
        manifest_hash="sha256:" + "e" * 64,
        image_digest="sha256:" + "f" * 64,
        signature_status="verified",
        package_path="plugins/packages/v1.1",
        publisher_id=publisher.id,
        manifest_json={"id": "com.example.diagnostic", "version": "1.1.0"},
    )
    repository.ensure_installation(
        plugin_id="com.example.diagnostic",
        preferred_version="1.0.0",
        status="disabled",
    )
    return first, second


def test_multiple_immutable_versions_and_one_preferred_version(database: Database) -> None:
    with Session(database.engine) as db_session:
        repository = PluginRepository(db_session)
        first, second = install_versions(repository)
        installation = repository.set_preferred_version(
            plugin_id="com.example.diagnostic",
            version="1.1.0",
        )
        db_session.commit()

        packages = repository.list_packages("com.example.diagnostic")
        assert [item.version for item in packages] == ["1.0.0", "1.1.0"]
        assert installation.preferred_package_id == second.id
        assert installation.preferred_package_id != first.id
        with pytest.raises(ValueError, match="immutable"):
            repository.record_package(
                plugin_id="com.example.diagnostic",
                version="1.1.0",
                content_digest="sha256:" + "9" * 64,
                manifest_hash="sha256:" + "8" * 64,
                image_digest="sha256:" + "7" * 64,
                signature_status="verified",
                package_path="changed",
                publisher_id=second.publisher_id,
                manifest_json={"changed": True},
            )


def test_publisher_fingerprint_is_pinned(database: Database) -> None:
    with Session(database.engine) as db_session:
        repository = PluginRepository(db_session)
        trusted = repository.trust_publisher(
            name="Example Publisher",
            key_fingerprint="sha256:" + "1" * 64,
            public_key="key-1",
        )
        repeated = repository.trust_publisher(
            name="Example Publisher",
            key_fingerprint="sha256:" + "1" * 64,
            public_key="key-1",
        )
        assert trusted.id == repeated.id
        with pytest.raises(ValueError, match="fingerprint"):
            repository.trust_publisher(
                name="Example Publisher",
                key_fingerprint="sha256:" + "2" * 64,
                public_key="key-2",
            )


def test_staging_ticket_expiry_and_one_time_consumption(database: Database) -> None:
    now = datetime.now(UTC)
    with Session(database.engine) as db_session:
        repository = PluginRepository(db_session)
        valid = repository.create_staging_ticket(
            plugin_id="com.example.diagnostic",
            version="1.0.0",
            manifest_hash="sha256:" + "a" * 64,
            content_digest="sha256:" + "b" * 64,
            staged_path="plugins/staging/ticket-1",
            permission_request=["ui.publish"],
            signature_status="verified",
            publisher_name="Example Publisher",
            publisher_fingerprint="sha256:" + "1" * 64,
            expires_at=now + timedelta(minutes=5),
        )
        consumed = repository.consume_staging_ticket(valid.id, now=now)
        assert consumed.consumed_at is not None
        with pytest.raises(StagingTicketError, match="consumed"):
            repository.consume_staging_ticket(valid.id, now=now)

        expired = repository.create_staging_ticket(
            plugin_id="com.example.diagnostic",
            version="1.1.0",
            manifest_hash="sha256:" + "c" * 64,
            content_digest="sha256:" + "d" * 64,
            staged_path="plugins/staging/ticket-2",
            permission_request=[],
            signature_status="verified",
            publisher_name="Example Publisher",
            publisher_fingerprint="sha256:" + "1" * 64,
            expires_at=now - timedelta(seconds=1),
        )
        with pytest.raises(StagingTicketError, match="expired"):
            repository.consume_staging_ticket(expired.id, now=now)


def test_permissions_health_binding_and_cursor_are_version_scoped(database: Database) -> None:
    with Session(database.engine) as db_session:
        media_session_id = seed_media_session(db_session)
        repository = PluginRepository(db_session)
        install_versions(repository)
        permissions = repository.replace_base_permissions(
            plugin_id="com.example.diagnostic",
            version="1.0.0",
            permissions=("media.transcript.final.read", "ui.publish"),
        )
        health = repository.record_runtime_status(
            plugin_id="com.example.diagnostic",
            version="1.0.0",
            status="crashed",
            crashed=True,
            exit_code=17,
        )
        binding = repository.bind_session(
            plugin_id="com.example.diagnostic",
            version="1.0.0",
            media_session_id=media_session_id,
            session_scope="scope_opaque_123456789",
        )
        repository.advance_binding_delivery(binding.id, sequence=8)
        repository.acknowledge_binding(binding.id, sequence=5)
        stale = repository.acknowledge_binding(binding.id, sequence=3)
        rotated = repository.bind_session(
            plugin_id="com.example.diagnostic",
            version="1.0.0",
            media_session_id=media_session_id,
            session_scope="scope_rotated_123456789",
        )
        db_session.commit()

        assert {item.permission_name for item in permissions} == {
            "media.transcript.final.read",
            "ui.publish",
        }
        assert health.crash_count == 1
        assert health.last_exit_code == 17
        assert binding.package_id == repository.get_package(
            "com.example.diagnostic", "1.0.0"
        ).id
        assert stale.last_delivered_sequence == 8
        assert stale.last_acknowledged_sequence == 5
        assert rotated.session_scope == "scope_rotated_123456789"
        assert rotated.last_acknowledged_sequence == 5
        assert repository.list_open_bindings(
            plugin_id="com.example.diagnostic", version="1.0.0"
        ) == [rotated]


def test_namespaced_state_uses_optimistic_versions_and_quota(database: Database) -> None:
    with Session(database.engine) as db_session:
        repository = PluginRepository(db_session)
        install_versions(repository)
        created = repository.put_state(
            plugin_id="com.example.diagnostic",
            version="1.0.0",
            namespace="session:one",
            key="summary",
            value={"text": "v1"},
            expected_version=0,
            quota_bytes=1_000,
        )
        updated = repository.put_state(
            plugin_id="com.example.diagnostic",
            version="1.0.0",
            namespace="session:one",
            key="summary",
            value={"text": "v2"},
            expected_version=1,
            quota_bytes=1_000,
        )
        assert created.id == updated.id
        assert updated.version == 2
        with pytest.raises(PluginStateConflictError):
            repository.put_state(
                plugin_id="com.example.diagnostic",
                version="1.0.0",
                namespace="session:one",
                key="summary",
                value={"text": "stale"},
                expected_version=1,
                quota_bytes=1_000,
            )
        with pytest.raises(PluginStateQuotaError):
            repository.put_state(
                plugin_id="com.example.diagnostic",
                version="1.0.0",
                namespace="session:one",
                key="large",
                value={"text": "x" * 200},
                expected_version=0,
                quota_bytes=64,
            )


def test_ui_grants_invocations_and_audit_are_durable_and_idempotent(database: Database) -> None:
    now = datetime.now(UTC)
    with Session(database.engine) as db_session:
        media_session_id = seed_media_session(db_session)
        repository = PluginRepository(db_session)
        install_versions(repository)
        first_view = repository.publish_view(
            plugin_id="com.example.diagnostic",
            version="1.0.0",
            media_session_id=media_session_id,
            surface="panel",
            view_id="main",
            view_version=1,
            view_json={"type": "text", "text": "ready"},
        )
        updated_view = repository.publish_view(
            plugin_id="com.example.diagnostic",
            version="1.0.0",
            media_session_id=media_session_id,
            surface="panel",
            view_id="main",
            view_version=2,
            view_json={"type": "text", "text": "updated"},
        )
        assert first_view.id == updated_view.id
        repeated_view = repository.publish_view(
            plugin_id="com.example.diagnostic",
            version="1.0.0",
            media_session_id=media_session_id,
            surface="panel",
            view_id="main",
            view_version=2,
            view_json={"type": "text", "text": "updated"},
        )
        assert repeated_view.id == updated_view.id
        with pytest.raises(ValueError, match="version"):
            repository.publish_view(
                plugin_id="com.example.diagnostic",
                version="1.0.0",
                media_session_id=media_session_id,
                surface="panel",
                view_id="main",
                view_version=2,
                view_json={"type": "text", "text": "stale"},
            )

        grant = repository.create_capability_grant(
            plugin_id="com.example.diagnostic",
            version="1.0.0",
            media_session_id=media_session_id,
            capability="network.fetch",
            effect="network",
            scope={"destinations": ["api.example.com"]},
            expires_at=now + timedelta(minutes=5),
        )
        invocation = repository.create_capability_invocation(
            plugin_id="com.example.diagnostic",
            version="1.0.0",
            media_session_id=media_session_id,
            capability="network.fetch",
            effect="network",
            idempotency_key="request-1",
            request={"url": "https://api.example.com/data"},
        )
        repeated = repository.create_capability_invocation(
            plugin_id="com.example.diagnostic",
            version="1.0.0",
            media_session_id=media_session_id,
            capability="network.fetch",
            effect="network",
            idempotency_key="request-1",
            request={"url": "https://api.example.com/data"},
        )
        audit = repository.append_audit_event(
            plugin_id="com.example.diagnostic",
            version="1.0.0",
            media_session_id=media_session_id,
            event_type="capability.requested",
            severity="info",
            payload={"invocation_id": invocation.id, "grant_id": grant.id},
        )
        db_session.commit()

        assert invocation.id == repeated.id
        assert db_session.scalar(select(PluginCapabilityInvocationRecord)).id == invocation.id
        assert db_session.scalar(select(PluginAuditEventRecord)).id == audit.id
        assert len(list(db_session.scalars(select(PluginPermissionRecord)))) == 0
