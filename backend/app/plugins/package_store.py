from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from pydantic import ValidationError

from app.contract_versions import HOST_API_VERSION
from app.persistence.database import Database
from app.persistence.models import PluginPackageRecord, PluginStagingTicketRecord, utc_now
from app.plugins.contracts import PluginManifest
from app.plugins.manifest import host_api_compatible, load_manifest
from app.plugins.repository import PluginRepository, StagingTicketError
from app.plugins.signing import (
    SignatureEnvelope,
    assets_digest,
    canonical_json_bytes,
    public_key_fingerprint,
    verify_signature,
)


class PackageInspectionError(ValueError):
    pass


class ImageImporter(Protocol):
    async def import_image(self, image_tar: Path, expected_digest: str) -> object: ...


@dataclass(frozen=True, slots=True)
class PackageStoreConfig:
    packages_dir: Path
    staging_dir: Path
    allow_unsigned: bool = False
    max_entries: int = 2_000
    max_compressed_bytes: int = 512 * 1024 * 1024
    max_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024
    max_compression_ratio: float = 200.0
    ticket_ttl_seconds: int = 300
    host_api_version: str = HOST_API_VERSION

    def __post_init__(self) -> None:
        if self.max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if self.max_compressed_bytes <= 0 or self.max_uncompressed_bytes <= 0:
            raise ValueError("package byte limits must be positive")
        if self.max_compression_ratio <= 0:
            raise ValueError("max_compression_ratio must be positive")
        if self.ticket_ttl_seconds <= 0:
            raise ValueError("ticket_ttl_seconds must be positive")


@dataclass(frozen=True, slots=True)
class PackageInspectionResult:
    ticket_id: str
    plugin_id: str
    version: str
    name: str
    publisher_name: str
    publisher_fingerprint: str | None
    publisher_trusted: bool
    signature_status: str
    permissions: tuple[str, ...]
    content_digest: str
    manifest_hash: str
    expires_at: dt.datetime


@dataclass(frozen=True, slots=True)
class _VerifiedPackage:
    manifest: PluginManifest
    manifest_json: dict[str, object]
    manifest_hash: str
    image_digest: str
    assets_digest: str
    signature_status: str
    publisher_name: str
    publisher_fingerprint: str | None
    publisher_public_key: str | None
    content_digest: str
    contents_dir: Path


class PackageStore:
    def __init__(
        self,
        database: Database,
        *,
        image_importer: ImageImporter,
        config: PackageStoreConfig,
    ) -> None:
        self._database = database
        self._image_importer = image_importer
        self._config = config
        config.packages_dir.mkdir(parents=True, exist_ok=True)
        config.staging_dir.mkdir(parents=True, exist_ok=True)

    def inspect(self, package_path: Path) -> PackageInspectionResult:
        path = package_path.resolve(strict=True)
        staging_root = Path(
            tempfile.mkdtemp(prefix="inspect-", dir=self._config.staging_dir)
        )
        try:
            staged_archive = staging_root / "package.plugin.zip"
            shutil.copyfile(path, staged_archive)
            contents_dir = staging_root / "contents"
            verified = self._verify_and_extract(staged_archive, contents_dir)
            with self._database.session() as db_session:
                repository = PluginRepository(db_session)
                trusted = (
                    repository.get_publisher_by_fingerprint(
                        verified.publisher_fingerprint
                    )
                    if verified.publisher_fingerprint is not None
                    else None
                )
                expires_at = utc_now() + dt.timedelta(
                    seconds=self._config.ticket_ttl_seconds
                )
                ticket = repository.create_staging_ticket(
                    plugin_id=verified.manifest.id,
                    version=verified.manifest.version,
                    manifest_hash=verified.manifest_hash,
                    content_digest=verified.content_digest,
                    staged_path=str(staging_root),
                    permission_request=verified.manifest.permissions,
                    signature_status=verified.signature_status,
                    publisher_name=verified.publisher_name,
                    publisher_fingerprint=verified.publisher_fingerprint,
                    expires_at=expires_at,
                )
                db_session.commit()
            return PackageInspectionResult(
                ticket_id=ticket.id,
                plugin_id=verified.manifest.id,
                version=verified.manifest.version,
                name=verified.manifest.name,
                publisher_name=verified.publisher_name,
                publisher_fingerprint=verified.publisher_fingerprint,
                publisher_trusted=trusted is not None,
                signature_status=verified.signature_status,
                permissions=verified.manifest.permissions,
                content_digest=verified.content_digest,
                manifest_hash=verified.manifest_hash,
                expires_at=expires_at,
            )
        except Exception:
            shutil.rmtree(staging_root, ignore_errors=True)
            raise

    async def confirm_install(
        self,
        ticket_id: str,
        *,
        accepted_permissions: tuple[str, ...],
        trust_publisher: bool = False,
        approved_publisher_fingerprint: str | None = None,
    ) -> PluginPackageRecord:
        staging_root: Path | None = None
        try:
            ticket = self._load_live_ticket(ticket_id)
            staging_root = Path(ticket.staged_path)
            staged_archive = staging_root / "package.plugin.zip"
            revalidated_dir = staging_root / "revalidated"
            if revalidated_dir.exists():
                shutil.rmtree(revalidated_dir)
            verified = self._verify_and_extract(staged_archive, revalidated_dir)
            self._match_ticket(ticket, verified)
            if tuple(sorted(set(accepted_permissions))) != verified.manifest.permissions:
                raise PackageInspectionError(
                    "accepted permissions must match the inspected permission request"
                )

            publisher_id: str | None = None
            with self._database.session() as db_session:
                repository = PluginRepository(db_session)
                if verified.publisher_fingerprint is not None:
                    publisher = repository.get_publisher_by_fingerprint(
                        verified.publisher_fingerprint
                    )
                    if publisher is None:
                        if not trust_publisher:
                            raise PackageInspectionError(
                                "publisher is not trusted; explicit trust is required"
                            )
                        if (
                            approved_publisher_fingerprint
                            != verified.publisher_fingerprint
                        ):
                            raise PackageInspectionError(
                                "approved publisher fingerprint does not match"
                            )
                        assert verified.publisher_public_key is not None
                        publisher = repository.trust_publisher(
                            name=verified.publisher_name,
                            key_fingerprint=verified.publisher_fingerprint,
                            public_key=verified.publisher_public_key,
                        )
                    publisher_id = publisher.id
                    db_session.commit()
                elif not self._config.allow_unsigned:
                    raise PackageInspectionError("unsigned plugin packages are disabled")

            destination = self._install_contents(verified)
            imported_image = await self._image_importer.import_image(
                destination / "image.tar",
                verified.image_digest,
            )
            runtime_image_ref = getattr(imported_image, "image_ref", None)
            if runtime_image_ref is not None and not isinstance(runtime_image_ref, str):
                raise PackageInspectionError("image importer returned an invalid image reference")

            with self._database.session() as db_session:
                repository = PluginRepository(db_session)
                package = repository.record_package(
                    plugin_id=verified.manifest.id,
                    version=verified.manifest.version,
                    content_digest=verified.content_digest,
                    manifest_hash=verified.manifest_hash,
                    image_digest=verified.image_digest,
                    signature_status=verified.signature_status,
                    package_path=str(destination),
                    publisher_id=publisher_id,
                    manifest_json=verified.manifest_json,
                    runtime_image_ref=runtime_image_ref,
                )
                repository.ensure_installation(
                    plugin_id=verified.manifest.id,
                    preferred_version=verified.manifest.version,
                    status="disabled",
                )
                repository.replace_base_permissions(
                    plugin_id=verified.manifest.id,
                    version=verified.manifest.version,
                    permissions=verified.manifest.permissions,
                )
                repository.record_runtime_status(
                    plugin_id=verified.manifest.id,
                    version=verified.manifest.version,
                    status="installed",
                )
                repository.consume_staging_ticket(ticket_id)
                db_session.commit()
                return package
        finally:
            if staging_root is not None:
                shutil.rmtree(staging_root, ignore_errors=True)

    def _load_live_ticket(self, ticket_id: str) -> PluginStagingTicketRecord:
        with self._database.session() as db_session:
            ticket = PluginRepository(db_session).get_staging_ticket(ticket_id)
            if ticket is None:
                raise PackageInspectionError("staging ticket does not exist")
            if ticket.consumed_at is not None:
                raise PackageInspectionError("staging ticket has already been consumed")
            expires_at = ticket.expires_at
            if expires_at.tzinfo is None or expires_at.utcoffset() is None:
                expires_at = expires_at.replace(tzinfo=dt.UTC)
            if expires_at <= utc_now():
                raise PackageInspectionError("staging ticket has expired")
            db_session.expunge(ticket)
            return ticket

    def _verify_and_extract(self, archive_path: Path, contents_dir: Path) -> _VerifiedPackage:
        content_digest = self._hash_file(archive_path)
        try:
            with zipfile.ZipFile(archive_path) as archive:
                infos = archive.infolist()
                self._validate_entries(infos)
                contents_dir.mkdir(parents=True, exist_ok=False)
                for info in infos:
                    normalized = self._safe_member_name(info.filename)
                    target = contents_dir / Path(*PurePosixPath(normalized).parts)
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, target.open("xb") as destination:
                        shutil.copyfileobj(source, destination, length=1024 * 1024)
        except (OSError, zipfile.BadZipFile, RuntimeError) as error:
            raise PackageInspectionError("plugin package is not a valid safe ZIP") from error

        try:
            manifest, manifest_json = load_manifest(
                (contents_dir / "plugin.json").read_bytes()
            )
        except (OSError, ValueError) as error:
            raise PackageInspectionError(str(error)) from error
        if not host_api_compatible(manifest.host_api, self._config.host_api_version):
            raise PackageInspectionError("plugin is incompatible with this Host API")

        manifest_hash = "sha256:" + hashlib.sha256(
            canonical_json_bytes(manifest_json)
        ).hexdigest()
        image_digest = self._hash_file(contents_dir / "image.tar")
        if manifest.image_digest != image_digest:
            raise PackageInspectionError("plugin image digest does not match image.tar")
        asset_values = {
            path.relative_to(contents_dir).as_posix(): path.read_bytes()
            for path in (contents_dir / "assets").rglob("*")
            if path.is_file()
        } if (contents_dir / "assets").is_dir() else {}
        package_assets_digest = assets_digest(asset_values)

        signature_path = contents_dir / "signature.json"
        if not signature_path.exists():
            if not self._config.allow_unsigned:
                raise PackageInspectionError("unsigned plugin packages are disabled")
            return _VerifiedPackage(
                manifest=manifest,
                manifest_json=manifest_json,
                manifest_hash=manifest_hash,
                image_digest=image_digest,
                assets_digest=package_assets_digest,
                signature_status="unsigned",
                publisher_name=manifest.publisher,
                publisher_fingerprint=None,
                publisher_public_key=None,
                content_digest=content_digest,
                contents_dir=contents_dir,
            )
        try:
            raw_signature = json.loads(signature_path.read_bytes())
            envelope = SignatureEnvelope.model_validate(raw_signature)
            if envelope.publisher != manifest.publisher:
                raise ValueError("signature publisher does not match manifest")
            if envelope.assets_digest != package_assets_digest:
                raise ValueError("signed assets digest does not match package assets")
            verify_signature(
                envelope,
                manifest=manifest_json,
                image_digest=image_digest,
                package_assets_digest=package_assets_digest,
            )
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError) as error:
            raise PackageInspectionError("plugin package signature is invalid") from error
        return _VerifiedPackage(
            manifest=manifest,
            manifest_json=manifest_json,
            manifest_hash=manifest_hash,
            image_digest=image_digest,
            assets_digest=package_assets_digest,
            signature_status="verified",
            publisher_name=envelope.publisher,
            publisher_fingerprint=public_key_fingerprint(envelope.public_key),
            publisher_public_key=envelope.public_key,
            content_digest=content_digest,
            contents_dir=contents_dir,
        )

    def _validate_entries(self, infos: list[zipfile.ZipInfo]) -> None:
        if len(infos) > self._config.max_entries:
            raise PackageInspectionError("plugin package has too many entries")
        names: set[str] = set()
        total_compressed = 0
        total_uncompressed = 0
        for info in infos:
            normalized = self._safe_member_name(info.filename)
            key = normalized.casefold()
            if key in names:
                raise PackageInspectionError("plugin package contains duplicate entries")
            names.add(key)
            unix_mode = info.external_attr >> 16
            if stat.S_ISLNK(unix_mode):
                raise PackageInspectionError("plugin package cannot contain links")
            total_compressed += info.compress_size
            total_uncompressed += info.file_size
            if info.file_size and info.file_size / max(info.compress_size, 1) > self._config.max_compression_ratio:
                raise PackageInspectionError("plugin package exceeds compression ratio limit")
        if total_compressed > self._config.max_compressed_bytes:
            raise PackageInspectionError("plugin package exceeds compressed size limit")
        if total_uncompressed > self._config.max_uncompressed_bytes:
            raise PackageInspectionError("plugin package exceeds uncompressed size limit")
        if not {"plugin.json", "image.tar"}.issubset(names):
            raise PackageInspectionError("plugin package is missing required entries")

    @staticmethod
    def _safe_member_name(name: str) -> str:
        if not name or "\x00" in name or "\\" in name:
            raise PackageInspectionError("plugin package contains an unsafe path")
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
            raise PackageInspectionError("plugin package contains an absolute path")
        path = PurePosixPath(name)
        if any(part in {"", ".", ".."} for part in path.parts):
            raise PackageInspectionError("plugin package contains path traversal")
        return path.as_posix()

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return "sha256:" + digest.hexdigest()

    @staticmethod
    def _match_ticket(
        ticket: PluginStagingTicketRecord,
        verified: _VerifiedPackage,
    ) -> None:
        if (
            ticket.plugin_id != verified.manifest.id
            or ticket.version != verified.manifest.version
            or ticket.manifest_hash != verified.manifest_hash
            or ticket.content_digest != verified.content_digest
            or ticket.signature_status != verified.signature_status
            or ticket.publisher_fingerprint != verified.publisher_fingerprint
        ):
            raise PackageInspectionError("staged package no longer matches inspection ticket")

    def _install_contents(self, verified: _VerifiedPackage) -> Path:
        digest_component = verified.content_digest.removeprefix("sha256:")
        destination = (
            self._config.packages_dir
            / verified.manifest.id
            / verified.manifest.version
            / digest_component
        )
        if destination.exists():
            installed_manifest_hash = "sha256:" + hashlib.sha256(
                canonical_json_bytes(
                    json.loads((destination / "plugin.json").read_bytes())
                )
            ).hexdigest()
            if installed_manifest_hash != verified.manifest_hash:
                raise PackageInspectionError("immutable package destination was modified")
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.parent / f".install-{uuid.uuid4().hex}"
        try:
            shutil.copytree(verified.contents_dir, temporary)
            os.replace(temporary, destination)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        return destination
