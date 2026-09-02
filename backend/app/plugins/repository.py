from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.persistence.models import (
    MediaSessionRecord,
    PluginAuditEventRecord,
    PluginCapabilityGrantRecord,
    PluginCapabilityInvocationRecord,
    PluginInstallationRecord,
    PluginPackageRecord,
    PluginPermissionRecord,
    PluginPublisherRecord,
    PluginRuntimeHealthRecord,
    PluginSessionBindingRecord,
    PluginStagingTicketRecord,
    PluginStateItemRecord,
    PluginUIViewRecord,
    utc_now,
)


class StagingTicketError(ValueError):
    pass


class PluginStateConflictError(ValueError):
    pass


class PluginStateQuotaError(ValueError):
    pass


def _aware(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class PluginRepository:
    """Transaction-scoped persistence for plugin installation and runtime state."""

    def __init__(self, db_session: Session) -> None:
        self._db = db_session

    def trust_publisher(
        self,
        *,
        name: str,
        key_fingerprint: str,
        public_key: str,
    ) -> PluginPublisherRecord:
        existing = self._db.scalar(
            select(PluginPublisherRecord).where(PluginPublisherRecord.name == name)
        )
        if existing is not None:
            if (
                existing.key_fingerprint != key_fingerprint
                or existing.public_key != public_key
            ):
                raise ValueError("publisher fingerprint is already pinned")
            if existing.revoked_at is not None:
                raise ValueError("publisher key has been revoked")
            return existing
        fingerprint_owner = self._db.scalar(
            select(PluginPublisherRecord).where(
                PluginPublisherRecord.key_fingerprint == key_fingerprint
            )
        )
        if fingerprint_owner is not None:
            raise ValueError("publisher fingerprint is already pinned to another name")
        record = PluginPublisherRecord(
            id=str(uuid.uuid4()),
            name=name,
            key_fingerprint=key_fingerprint,
            public_key=public_key,
            trusted_at=utc_now(),
        )
        self._db.add(record)
        self._db.flush()
        return record

    def get_publisher_by_fingerprint(
        self,
        key_fingerprint: str,
    ) -> PluginPublisherRecord | None:
        return self._db.scalar(
            select(PluginPublisherRecord).where(
                PluginPublisherRecord.key_fingerprint == key_fingerprint,
                PluginPublisherRecord.revoked_at.is_(None),
            )
        )

    def record_package(
        self,
        *,
        plugin_id: str,
        version: str,
        content_digest: str,
        manifest_hash: str,
        image_digest: str,
        signature_status: str,
        package_path: str,
        publisher_id: str | None,
        manifest_json: dict[str, object],
        runtime_image_ref: str | None = None,
    ) -> PluginPackageRecord:
        existing = self.get_package(plugin_id, version, required=False)
        if existing is not None:
            if existing.content_digest != content_digest:
                raise ValueError("installed plugin versions are immutable")
            return existing
        record = PluginPackageRecord(
            id=str(uuid.uuid4()),
            plugin_id=plugin_id,
            version=version,
            content_digest=content_digest,
            manifest_hash=manifest_hash,
            image_digest=image_digest,
            runtime_image_ref=runtime_image_ref,
            signature_status=signature_status,
            package_path=package_path,
            publisher_id=publisher_id,
            manifest_json=manifest_json,
        )
        self._db.add(record)
        self._db.flush()
        return record

    def get_package(
        self,
        plugin_id: str,
        version: str,
        *,
        required: bool = True,
    ) -> PluginPackageRecord | None:
        record = self._db.scalar(
            select(PluginPackageRecord).where(
                PluginPackageRecord.plugin_id == plugin_id,
                PluginPackageRecord.version == version,
            )
        )
        if required and record is None:
            raise LookupError(f"plugin package not installed: {plugin_id}@{version}")
        return record

    def list_packages(self, plugin_id: str) -> list[PluginPackageRecord]:
        return list(
            self._db.scalars(
                select(PluginPackageRecord)
                .where(PluginPackageRecord.plugin_id == plugin_id)
                .order_by(PluginPackageRecord.version)
            )
        )

    def ensure_installation(
        self,
        *,
        plugin_id: str,
        preferred_version: str,
        status: str,
    ) -> PluginInstallationRecord:
        package = self._require_package(plugin_id, preferred_version)
        existing = self._db.scalar(
            select(PluginInstallationRecord).where(
                PluginInstallationRecord.plugin_id == plugin_id
            )
        )
        if existing is not None:
            return existing
        record = PluginInstallationRecord(
            id=str(uuid.uuid4()),
            plugin_id=plugin_id,
            preferred_package_id=package.id,
            status=status,
        )
        self._db.add(record)
        self._db.flush()
        return record

    def list_open_bindings(
        self,
        *,
        plugin_id: str,
        version: str,
    ) -> list[PluginSessionBindingRecord]:
        package = self._require_package(plugin_id, version)
        return list(
            self._db.scalars(
                select(PluginSessionBindingRecord).where(
                    PluginSessionBindingRecord.package_id == package.id,
                    PluginSessionBindingRecord.status == "open",
                )
            )
        )

    def list_open_bindings_for_plugin(
        self,
        plugin_id: str,
    ) -> list[PluginSessionBindingRecord]:
        return list(
            self._db.scalars(
                select(PluginSessionBindingRecord).where(
                    PluginSessionBindingRecord.plugin_id == plugin_id,
                    PluginSessionBindingRecord.status == "open",
                )
            )
        )

    def get_open_binding_for_plugin_session(
        self,
        *,
        plugin_id: str,
        media_session_id: str,
    ) -> PluginSessionBindingRecord | None:
        return self._db.scalar(
            select(PluginSessionBindingRecord)
            .where(
                PluginSessionBindingRecord.plugin_id == plugin_id,
                PluginSessionBindingRecord.media_session_id == media_session_id,
                PluginSessionBindingRecord.status == "open",
            )
            .order_by(PluginSessionBindingRecord.opened_at)
            .limit(1)
        )

    def get_binding_for_identity(
        self,
        *,
        plugin_id: str,
        version: str,
        media_session_id: str,
    ) -> PluginSessionBindingRecord | None:
        package = self._require_package(plugin_id, version)
        return self._db.scalar(
            select(PluginSessionBindingRecord).where(
                PluginSessionBindingRecord.package_id == package.id,
                PluginSessionBindingRecord.media_session_id == media_session_id,
                PluginSessionBindingRecord.status == "open",
            )
        )

    def set_preferred_version(
        self,
        *,
        plugin_id: str,
        version: str,
    ) -> PluginInstallationRecord:
        package = self._require_package(plugin_id, version)
        installation = self._db.scalar(
            select(PluginInstallationRecord).where(
                PluginInstallationRecord.plugin_id == plugin_id
            )
        )
        if installation is None:
            raise LookupError(f"plugin installation does not exist: {plugin_id}")
        installation.preferred_package_id = package.id
        installation.updated_at = utc_now()
        self._db.flush()
        return installation

    def get_installation(
        self,
        plugin_id: str,
        *,
        required: bool = True,
    ) -> PluginInstallationRecord | None:
        record = self._db.scalar(
            select(PluginInstallationRecord).where(
                PluginInstallationRecord.plugin_id == plugin_id
            )
        )
        if required and record is None:
            raise LookupError(f"plugin installation does not exist: {plugin_id}")
        return record

    def list_installations(self) -> list[PluginInstallationRecord]:
        return list(
            self._db.scalars(
                select(PluginInstallationRecord).order_by(
                    PluginInstallationRecord.plugin_id
                )
            )
        )

    def preferred_package(self, plugin_id: str) -> PluginPackageRecord:
        installation = self.get_installation(plugin_id)
        assert installation is not None
        package = self._db.get(PluginPackageRecord, installation.preferred_package_id)
        if package is None:
            raise LookupError(f"preferred plugin package is missing: {plugin_id}")
        return package

    def set_installation_status(
        self,
        plugin_id: str,
        status: str,
    ) -> PluginInstallationRecord:
        installation = self.get_installation(plugin_id)
        assert installation is not None
        installation.status = status
        installation.updated_at = utc_now()
        self._db.flush()
        return installation

    def get_runtime_health(
        self,
        *,
        plugin_id: str,
        version: str,
    ) -> PluginRuntimeHealthRecord | None:
        package = self._require_package(plugin_id, version)
        return self._db.scalar(
            select(PluginRuntimeHealthRecord).where(
                PluginRuntimeHealthRecord.package_id == package.id
            )
        )

    def create_staging_ticket(
        self,
        *,
        plugin_id: str,
        version: str,
        manifest_hash: str,
        content_digest: str,
        staged_path: str,
        permission_request: list[str] | tuple[str, ...],
        signature_status: str,
        publisher_name: str | None,
        publisher_fingerprint: str | None,
        expires_at: dt.datetime,
    ) -> PluginStagingTicketRecord:
        record = PluginStagingTicketRecord(
            id=str(uuid.uuid4()),
            plugin_id=plugin_id,
            version=version,
            manifest_hash=manifest_hash,
            content_digest=content_digest,
            staged_path=staged_path,
            permission_request_json=list(permission_request),
            signature_status=signature_status,
            publisher_name=publisher_name,
            publisher_fingerprint=publisher_fingerprint,
            expires_at=_aware(expires_at),
        )
        self._db.add(record)
        self._db.flush()
        return record

    def get_staging_ticket(self, ticket_id: str) -> PluginStagingTicketRecord | None:
        return self._db.get(PluginStagingTicketRecord, ticket_id)

    def cleanup_expired_authority(
        self,
        *,
        now: dt.datetime | None = None,
    ) -> list[str]:
        current = _aware(now or utc_now())
        expired_paths = list(
            self._db.scalars(
                select(PluginStagingTicketRecord.staged_path).where(
                    PluginStagingTicketRecord.expires_at <= current
                )
            )
        )
        self._db.execute(
            delete(PluginStagingTicketRecord).where(
                PluginStagingTicketRecord.expires_at <= current
            )
        )
        self._db.execute(
            update(PluginCapabilityGrantRecord)
            .where(
                PluginCapabilityGrantRecord.status == "active",
                PluginCapabilityGrantRecord.expires_at <= current,
            )
            .values(status="expired")
        )
        self._db.flush()
        return expired_paths

    def consume_staging_ticket(
        self,
        ticket_id: str,
        *,
        now: dt.datetime | None = None,
    ) -> PluginStagingTicketRecord:
        ticket = self._db.get(PluginStagingTicketRecord, ticket_id)
        if ticket is None:
            raise StagingTicketError("staging ticket does not exist")
        current = _aware(now or utc_now())
        if ticket.consumed_at is not None:
            raise StagingTicketError("staging ticket has already been consumed")
        if _aware(ticket.expires_at) <= current:
            raise StagingTicketError("staging ticket has expired")
        ticket.consumed_at = current
        self._db.flush()
        return ticket

    def replace_base_permissions(
        self,
        *,
        plugin_id: str,
        version: str,
        permissions: tuple[str, ...],
    ) -> list[PluginPermissionRecord]:
        package = self._require_package(plugin_id, version)
        self._db.execute(
            delete(PluginPermissionRecord).where(
                PluginPermissionRecord.package_id == package.id
            )
        )
        records = [
            PluginPermissionRecord(
                id=str(uuid.uuid4()),
                plugin_id=plugin_id,
                plugin_version=version,
                package_id=package.id,
                permission_name=permission,
                status="accepted",
            )
            for permission in sorted(set(permissions))
        ]
        self._db.add_all(records)
        self._db.flush()
        return records

    def list_base_permissions(
        self,
        *,
        plugin_id: str,
        version: str,
    ) -> frozenset[str]:
        package = self._require_package(plugin_id, version)
        return frozenset(
            self._db.scalars(
                select(PluginPermissionRecord.permission_name).where(
                    PluginPermissionRecord.package_id == package.id,
                    PluginPermissionRecord.status == "accepted",
                )
            )
        )

    def revoke_base_permission(
        self,
        *,
        plugin_id: str,
        version: str,
        permission: str,
    ) -> PluginPermissionRecord:
        package = self._require_package(plugin_id, version)
        record = self._db.scalar(
            select(PluginPermissionRecord).where(
                PluginPermissionRecord.package_id == package.id,
                PluginPermissionRecord.permission_name == permission,
            )
        )
        if record is None:
            raise LookupError(f"plugin permission does not exist: {permission}")
        record.status = "revoked"
        self._db.flush()
        return record

    def record_runtime_status(
        self,
        *,
        plugin_id: str,
        version: str,
        status: str,
        crashed: bool = False,
        exit_code: int | None = None,
        quarantine_reason: str | None = None,
    ) -> PluginRuntimeHealthRecord:
        package = self._require_package(plugin_id, version)
        record = self._db.scalar(
            select(PluginRuntimeHealthRecord).where(
                PluginRuntimeHealthRecord.package_id == package.id
            )
        )
        now = utc_now()
        if record is None:
            record = PluginRuntimeHealthRecord(
                id=str(uuid.uuid4()),
                plugin_id=plugin_id,
                plugin_version=version,
                package_id=package.id,
                status=status,
                crash_count=1 if crashed else 0,
                last_exit_code=exit_code,
                quarantine_reason=quarantine_reason,
                updated_at=now,
            )
            self._db.add(record)
        else:
            record.status = status
            record.crash_count += 1 if crashed else 0
            record.last_exit_code = exit_code
            record.quarantine_reason = quarantine_reason
            record.updated_at = now
        if status == "starting":
            record.last_started_at = now
        if status in {"ready", "degraded"}:
            record.last_heartbeat_at = now
        self._db.flush()
        return record

    def bind_session(
        self,
        *,
        plugin_id: str,
        version: str,
        media_session_id: str,
        session_scope: str,
    ) -> PluginSessionBindingRecord:
        package = self._require_package(plugin_id, version)
        if self._db.get(MediaSessionRecord, media_session_id) is None:
            raise LookupError(f"MediaSession does not exist: {media_session_id}")
        existing = self._db.scalar(
            select(PluginSessionBindingRecord).where(
                PluginSessionBindingRecord.package_id == package.id,
                PluginSessionBindingRecord.media_session_id == media_session_id,
            )
        )
        if existing is not None:
            if existing.session_scope != session_scope:
                existing.session_scope = session_scope
                existing.status = "open"
                existing.closed_at = None
                existing.updated_at = utc_now()
                self._db.flush()
            return existing
        record = PluginSessionBindingRecord(
            id=str(uuid.uuid4()),
            plugin_id=plugin_id,
            plugin_version=version,
            package_id=package.id,
            media_session_id=media_session_id,
            session_scope=session_scope,
            status="open",
            last_delivered_sequence=0,
            last_acknowledged_sequence=0,
        )
        self._db.add(record)
        self._db.flush()
        return record

    def advance_binding_delivery(
        self,
        binding_id: str,
        *,
        sequence: int,
    ) -> PluginSessionBindingRecord:
        record = self._require_binding(binding_id)
        if sequence > record.last_delivered_sequence:
            record.last_delivered_sequence = sequence
            record.updated_at = utc_now()
            self._db.flush()
        return record

    def acknowledge_binding(
        self,
        binding_id: str,
        *,
        sequence: int,
    ) -> PluginSessionBindingRecord:
        record = self._require_binding(binding_id)
        if sequence > record.last_delivered_sequence:
            raise ValueError("cannot acknowledge an event that was not delivered")
        if sequence > record.last_acknowledged_sequence:
            record.last_acknowledged_sequence = sequence
            record.updated_at = utc_now()
            self._db.flush()
        return record

    def put_state(
        self,
        *,
        plugin_id: str,
        version: str,
        namespace: str,
        key: str,
        value: dict[str, object],
        expected_version: int,
        quota_bytes: int,
    ) -> PluginStateItemRecord:
        package = self._require_package(plugin_id, version)
        encoded = _canonical_json(value)
        record = self._db.scalar(
            select(PluginStateItemRecord).where(
                PluginStateItemRecord.package_id == package.id,
                PluginStateItemRecord.namespace == namespace,
                PluginStateItemRecord.item_key == key,
            )
        )
        current_version = record.version if record is not None else 0
        if current_version != expected_version:
            raise PluginStateConflictError(
                f"expected state version {expected_version}, found {current_version}"
            )
        current_size = record.size_bytes if record is not None else 0
        used = int(
            self._db.scalar(
                select(func.coalesce(func.sum(PluginStateItemRecord.size_bytes), 0)).where(
                    PluginStateItemRecord.package_id == package.id
                )
            )
            or 0
        )
        if used - current_size + len(encoded) > quota_bytes:
            raise PluginStateQuotaError("plugin state quota exceeded")
        now = utc_now()
        if record is None:
            record = PluginStateItemRecord(
                id=str(uuid.uuid4()),
                plugin_id=plugin_id,
                plugin_version=version,
                package_id=package.id,
                namespace=namespace,
                item_key=key,
                value_json=value,
                size_bytes=len(encoded),
                version=1,
                created_at=now,
                updated_at=now,
            )
            self._db.add(record)
        else:
            record.value_json = value
            record.size_bytes = len(encoded)
            record.version += 1
            record.updated_at = now
        self._db.flush()
        return record

    def get_state(
        self,
        *,
        plugin_id: str,
        version: str,
        namespace: str,
        key: str,
    ) -> PluginStateItemRecord | None:
        package = self._require_package(plugin_id, version)
        return self._db.scalar(
            select(PluginStateItemRecord).where(
                PluginStateItemRecord.package_id == package.id,
                PluginStateItemRecord.namespace == namespace,
                PluginStateItemRecord.item_key == key,
            )
        )

    def export_state(
        self,
        *,
        plugin_id: str,
        version: str,
    ) -> list[dict[str, object]]:
        package = self._require_package(plugin_id, version)
        records = self._db.scalars(
            select(PluginStateItemRecord)
            .where(PluginStateItemRecord.package_id == package.id)
            .order_by(PluginStateItemRecord.namespace, PluginStateItemRecord.item_key)
        )
        return [
            {
                "namespace": item.namespace,
                "key": item.item_key,
                "value": item.value_json,
                "version": item.version,
            }
            for item in records
        ]

    def replace_state_snapshot(
        self,
        *,
        plugin_id: str,
        version: str,
        items: list[dict[str, object]],
        quota_bytes: int,
    ) -> list[PluginStateItemRecord]:
        package = self._require_package(plugin_id, version)
        encoded_items: list[tuple[dict[str, object], bytes]] = []
        for item in items:
            value = item["value"]
            assert isinstance(value, dict)
            encoded_items.append((item, _canonical_json(value)))
        if sum(len(encoded) for _, encoded in encoded_items) > quota_bytes:
            raise PluginStateQuotaError("plugin state quota exceeded")
        self._db.execute(
            delete(PluginStateItemRecord).where(
                PluginStateItemRecord.package_id == package.id
            )
        )
        now = utc_now()
        records = [
            PluginStateItemRecord(
                id=str(uuid.uuid4()),
                plugin_id=plugin_id,
                plugin_version=version,
                package_id=package.id,
                namespace=str(item["namespace"]),
                item_key=str(item["key"]),
                value_json=item["value"],
                size_bytes=len(encoded),
                version=int(item["version"]),
                created_at=now,
                updated_at=now,
            )
            for item, encoded in encoded_items
        ]
        self._db.add_all(records)
        self._db.flush()
        return records

    def publish_view(
        self,
        *,
        plugin_id: str,
        version: str,
        media_session_id: str,
        surface: str,
        view_id: str,
        view_version: int,
        view_json: dict[str, object],
    ) -> PluginUIViewRecord:
        package = self._require_package(plugin_id, version)
        if self._db.get(MediaSessionRecord, media_session_id) is None:
            raise LookupError(f"MediaSession does not exist: {media_session_id}")
        record = self._db.scalar(
            select(PluginUIViewRecord).where(
                PluginUIViewRecord.package_id == package.id,
                PluginUIViewRecord.media_session_id == media_session_id,
                PluginUIViewRecord.surface == surface,
                PluginUIViewRecord.view_id == view_id,
            )
        )
        now = utc_now()
        if record is not None:
            if view_version == record.view_version and view_json == record.view_json:
                return record
            if view_version <= record.view_version:
                raise ValueError("view version must increase")
            record.view_version = view_version
            record.view_json = view_json
            record.updated_at = now
            self._db.flush()
            return record
        record = PluginUIViewRecord(
            id=str(uuid.uuid4()),
            plugin_id=plugin_id,
            plugin_version=version,
            package_id=package.id,
            media_session_id=media_session_id,
            surface=surface,
            view_id=view_id,
            view_version=view_version,
            view_json=view_json,
            created_at=now,
            updated_at=now,
        )
        self._db.add(record)
        self._db.flush()
        return record

    def get_view(
        self,
        *,
        plugin_id: str,
        version: str,
        media_session_id: str,
        surface: str,
        view_id: str,
    ) -> PluginUIViewRecord | None:
        package = self._require_package(plugin_id, version)
        return self._db.scalar(
            select(PluginUIViewRecord).where(
                PluginUIViewRecord.package_id == package.id,
                PluginUIViewRecord.media_session_id == media_session_id,
                PluginUIViewRecord.surface == surface,
                PluginUIViewRecord.view_id == view_id,
            )
        )

    def list_views_for_session(
        self,
        media_session_id: str,
    ) -> list[PluginUIViewRecord]:
        return list(
            self._db.scalars(
                select(PluginUIViewRecord)
                .where(PluginUIViewRecord.media_session_id == media_session_id)
                .order_by(
                    PluginUIViewRecord.plugin_id,
                    PluginUIViewRecord.surface,
                    PluginUIViewRecord.view_id,
                )
            )
        )

    def delete_plugin(self, plugin_id: str) -> list[str]:
        installation = self.get_installation(plugin_id)
        assert installation is not None
        package_paths = list(
            self._db.scalars(
                select(PluginPackageRecord.package_path).where(
                    PluginPackageRecord.plugin_id == plugin_id
                )
            )
        )
        for model in (
            PluginAuditEventRecord,
            PluginCapabilityInvocationRecord,
            PluginCapabilityGrantRecord,
            PluginUIViewRecord,
            PluginStateItemRecord,
            PluginSessionBindingRecord,
            PluginRuntimeHealthRecord,
            PluginPermissionRecord,
        ):
            self._db.execute(delete(model).where(model.plugin_id == plugin_id))
        self._db.execute(
            delete(PluginStagingTicketRecord).where(
                PluginStagingTicketRecord.plugin_id == plugin_id
            )
        )
        self._db.delete(installation)
        self._db.flush()
        self._db.execute(
            delete(PluginPackageRecord).where(
                PluginPackageRecord.plugin_id == plugin_id
            )
        )
        self._db.flush()
        return package_paths

    def create_capability_grant(
        self,
        *,
        plugin_id: str,
        version: str,
        media_session_id: str | None,
        capability: str,
        effect: str,
        scope: dict[str, object],
        expires_at: dt.datetime,
    ) -> PluginCapabilityGrantRecord:
        package = self._require_package(plugin_id, version)
        record = PluginCapabilityGrantRecord(
            id=str(uuid.uuid4()),
            plugin_id=plugin_id,
            plugin_version=version,
            package_id=package.id,
            media_session_id=media_session_id,
            capability=capability,
            effect=effect,
            scope_json=scope,
            status="active",
            expires_at=_aware(expires_at),
        )
        self._db.add(record)
        self._db.flush()
        return record

    def list_capability_grants(
        self,
        *,
        plugin_id: str,
        version: str,
        capability: str,
    ) -> list[PluginCapabilityGrantRecord]:
        package = self._require_package(plugin_id, version)
        return list(
            self._db.scalars(
                select(PluginCapabilityGrantRecord)
                .where(
                    PluginCapabilityGrantRecord.package_id == package.id,
                    PluginCapabilityGrantRecord.capability == capability,
                )
                .order_by(PluginCapabilityGrantRecord.created_at.desc())
            )
        )

    def create_capability_invocation(
        self,
        *,
        plugin_id: str,
        version: str,
        media_session_id: str | None,
        capability: str,
        effect: str,
        idempotency_key: str,
        request: dict[str, object],
    ) -> PluginCapabilityInvocationRecord:
        package = self._require_package(plugin_id, version)
        request_hash = hashlib.sha256(_canonical_json(request)).hexdigest()
        existing = self._db.scalar(
            select(PluginCapabilityInvocationRecord).where(
                PluginCapabilityInvocationRecord.plugin_id == plugin_id,
                PluginCapabilityInvocationRecord.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise ValueError("idempotency key was reused with a different request")
            if (
                existing.plugin_version != version
                or existing.media_session_id != media_session_id
                or existing.capability != capability
                or existing.effect != effect
            ):
                raise ValueError("idempotency key belongs to another capability scope")
            return existing
        record = PluginCapabilityInvocationRecord(
            id=str(uuid.uuid4()),
            plugin_id=plugin_id,
            plugin_version=version,
            package_id=package.id,
            media_session_id=media_session_id,
            capability=capability,
            effect=effect,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            request_json=request,
            status="pending",
            outcome_unknown=False,
        )
        self._db.add(record)
        self._db.flush()
        return record

    def get_capability_invocation(
        self,
        *,
        plugin_id: str,
        idempotency_key: str,
    ) -> PluginCapabilityInvocationRecord | None:
        return self._db.scalar(
            select(PluginCapabilityInvocationRecord).where(
                PluginCapabilityInvocationRecord.plugin_id == plugin_id,
                PluginCapabilityInvocationRecord.idempotency_key == idempotency_key,
            )
        )

    def count_capability_invocations(
        self,
        *,
        plugin_id: str,
        version: str,
        media_session_id: str | None,
        capability: str,
    ) -> int:
        package = self._require_package(plugin_id, version)
        return int(
            self._db.scalar(
                select(func.count(PluginCapabilityInvocationRecord.id)).where(
                    PluginCapabilityInvocationRecord.package_id == package.id,
                    PluginCapabilityInvocationRecord.media_session_id
                    == media_session_id,
                    PluginCapabilityInvocationRecord.capability == capability,
                )
            )
            or 0
        )

    def complete_capability_invocation(
        self,
        invocation_id: str,
        *,
        result: dict[str, object],
    ) -> PluginCapabilityInvocationRecord:
        record = self._require_invocation(invocation_id)
        record.status = "completed"
        record.result_json = result
        record.error_code = None
        record.outcome_unknown = False
        record.completed_at = utc_now()
        self._db.flush()
        return record

    def fail_capability_invocation(
        self,
        invocation_id: str,
        *,
        error_code: str,
        outcome_unknown: bool = False,
    ) -> PluginCapabilityInvocationRecord:
        record = self._require_invocation(invocation_id)
        record.status = "unknown" if outcome_unknown else "failed"
        record.result_json = None
        record.error_code = error_code
        record.outcome_unknown = outcome_unknown
        record.completed_at = utc_now()
        self._db.flush()
        return record

    def append_audit_event(
        self,
        *,
        plugin_id: str,
        version: str,
        media_session_id: str | None,
        event_type: str,
        severity: str,
        payload: dict[str, object],
    ) -> PluginAuditEventRecord:
        package = self._require_package(plugin_id, version)
        record = PluginAuditEventRecord(
            id=str(uuid.uuid4()),
            plugin_id=plugin_id,
            plugin_version=version,
            package_id=package.id,
            media_session_id=media_session_id,
            event_type=event_type,
            severity=severity,
            payload_json=payload,
        )
        self._db.add(record)
        self._db.flush()
        return record

    def _require_package(self, plugin_id: str, version: str) -> PluginPackageRecord:
        record = self.get_package(plugin_id, version)
        assert record is not None
        return record

    def _require_binding(self, binding_id: str) -> PluginSessionBindingRecord:
        record = self._db.get(PluginSessionBindingRecord, binding_id)
        if record is None:
            raise LookupError(f"plugin binding does not exist: {binding_id}")
        return record

    def _require_invocation(
        self,
        invocation_id: str,
    ) -> PluginCapabilityInvocationRecord:
        record = self._db.get(PluginCapabilityInvocationRecord, invocation_id)
        if record is None:
            raise LookupError(f"plugin capability invocation does not exist: {invocation_id}")
        return record
