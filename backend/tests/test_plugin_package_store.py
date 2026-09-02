from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import stat
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.orm import Session

from app.persistence.database import Database
from app.plugins.package_store import (
    PackageInspectionError,
    PackageStore,
    PackageStoreConfig,
)
from app.plugins.repository import PluginRepository
from app.plugins.signing import assets_digest, canonical_json_bytes, signed_material


class FakeImageImporter:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str]] = []

    async def import_image(self, image_tar: Path, expected_digest: str) -> object:
        self.calls.append((image_tar, expected_digest))
        return {"digest": expected_digest}


def package_bytes(
    tmp_path: Path,
    *,
    signed: bool = True,
    host_api: str = ">=1.0 <2.0",
    manifest_image_digest: str | None = None,
    mutate: str | None = None,
) -> tuple[Path, str, str, str]:
    image = b"OCI image tar for tests"
    assets = {"assets/readme.txt": b"safe asset"}
    image_digest = "sha256:" + hashlib.sha256(image).hexdigest()
    manifest = {
        "schema_version": 1,
        "id": "com.example.diagnostic",
        "name": "Diagnostic Plugin",
        "version": "1.0.0",
        "publisher": "Example Publisher",
        "host_api": host_api,
        "image_digest": manifest_image_digest or image_digest,
        "subscriptions": ["transcript.final"],
        "permissions": ["media.transcript.final.read", "ui.publish"],
        "commands": ["diagnose"],
        "ui_schema_version": 1,
        "state_schema_version": 1,
    }
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    public_key_text = base64.b64encode(public_key).decode("ascii")
    fingerprint = "sha256:" + hashlib.sha256(public_key).hexdigest()
    digest = assets_digest(assets)
    signature = private_key.sign(signed_material(manifest, image_digest, digest))
    signature_json = {
        "schema_version": 1,
        "algorithm": "Ed25519",
        "publisher": "Example Publisher",
        "public_key": public_key_text,
        "assets_digest": digest,
        "signature": base64.b64encode(signature).decode("ascii"),
    }

    package = tmp_path / f"plugin-{mutate or 'valid'}.plugin.zip"
    manifest_bytes = canonical_json_bytes(manifest)
    image_bytes = image
    asset_bytes = assets["assets/readme.txt"]
    signature_bytes = canonical_json_bytes(signature_json)
    if mutate == "manifest":
        changed = dict(manifest)
        changed["name"] = "Tampered"
        manifest_bytes = canonical_json_bytes(changed)
    elif mutate == "image":
        image_bytes += b"tampered"
    elif mutate == "asset":
        asset_bytes += b"tampered"
    elif mutate == "signature":
        changed_signature = dict(signature_json)
        changed_signature["signature"] = base64.b64encode(b"0" * 64).decode("ascii")
        signature_bytes = canonical_json_bytes(changed_signature)

    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("plugin.json", manifest_bytes)
        archive.writestr("image.tar", image_bytes)
        archive.writestr("assets/readme.txt", asset_bytes)
        if signed:
            archive.writestr("signature.json", signature_bytes)
    return package, fingerprint, public_key_text, image_digest


def make_store(
    tmp_path: Path,
    database: Database,
    importer: FakeImageImporter,
    **overrides: object,
) -> PackageStore:
    values: dict[str, object] = {
        "packages_dir": tmp_path / "installed",
        "staging_dir": tmp_path / "staging",
        "allow_unsigned": False,
        "max_entries": 100,
        "max_compressed_bytes": 2_000_000,
        "max_uncompressed_bytes": 4_000_000,
        "max_compression_ratio": 100,
        "ticket_ttl_seconds": 300,
    }
    values.update(overrides)
    return PackageStore(
        database,
        image_importer=importer,
        config=PackageStoreConfig(**values),
    )


def test_valid_signed_package_requires_explicit_trust_then_installs_immutably(
    tmp_path: Path,
) -> None:
    database = Database("sqlite://")
    database.create_schema()
    importer = FakeImageImporter()
    try:
        package, fingerprint, _public_key, image_digest = package_bytes(tmp_path)
        store = make_store(tmp_path, database, importer)
        inspection = store.inspect(package)
        assert inspection.signature_status == "verified"
        assert inspection.publisher_fingerprint == fingerprint
        assert inspection.publisher_trusted is False

        installed = asyncio.run(
            store.confirm_install(
                inspection.ticket_id,
                accepted_permissions=inspection.permissions,
                trust_publisher=True,
                approved_publisher_fingerprint=fingerprint,
            )
        )
        assert installed.plugin_id == "com.example.diagnostic"
        assert installed.version == "1.0.0"
        assert Path(installed.package_path).is_dir()
        assert importer.calls[0][1] == image_digest

        second = store.inspect(package)
        repeated = asyncio.run(
            store.confirm_install(
                second.ticket_id,
                accepted_permissions=second.permissions,
            )
        )
        assert repeated.id == installed.id
        assert repeated.package_path == installed.package_path
    finally:
        database.dispose()


@pytest.mark.parametrize("mutation", ["manifest", "image", "asset", "signature"])
def test_tampered_signed_material_is_rejected(tmp_path: Path, mutation: str) -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        package, *_ = package_bytes(tmp_path, mutate=mutation)
        store = make_store(tmp_path, database, FakeImageImporter())
        with pytest.raises(PackageInspectionError):
            store.inspect(package)
    finally:
        database.dispose()


def test_unsigned_requires_explicit_development_override(tmp_path: Path) -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        package, *_ = package_bytes(tmp_path, signed=False)
        with pytest.raises(PackageInspectionError, match="unsigned"):
            make_store(tmp_path, database, FakeImageImporter()).inspect(package)
        allowed = make_store(
            tmp_path,
            database,
            FakeImageImporter(),
            allow_unsigned=True,
        ).inspect(package)
        assert allowed.signature_status == "unsigned"
        assert allowed.publisher_trusted is False
    finally:
        database.dispose()


def test_unknown_publisher_cannot_install_without_explicit_trust(tmp_path: Path) -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        package, fingerprint, *_ = package_bytes(tmp_path)
        store = make_store(tmp_path, database, FakeImageImporter())
        inspection = store.inspect(package)
        with pytest.raises(PackageInspectionError, match="publisher"):
            asyncio.run(
                store.confirm_install(
                    inspection.ticket_id,
                    accepted_permissions=inspection.permissions,
                )
            )
        with Session(database.engine) as db_session:
            assert PluginRepository(db_session).get_publisher_by_fingerprint(fingerprint) is None
    finally:
        database.dispose()


@pytest.mark.parametrize(
    ("entry_name", "expected_error"),
    [
        ("../escape.txt", "path"),
        ("/absolute.txt", "path"),
        ("C:/drive.txt", "path"),
        # zipfile normalizes a Windows separator to '/'; the added unsigned
        # asset is still rejected because it invalidates the signed material.
        ("assets\\windows.txt", "path|signature"),
    ],
)
def test_archive_path_attacks_are_rejected(
    tmp_path: Path,
    entry_name: str,
    expected_error: str,
) -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        package, *_ = package_bytes(tmp_path)
        with zipfile.ZipFile(package, "a") as archive:
            archive.writestr(entry_name, b"attack")
        with pytest.raises(PackageInspectionError, match=expected_error):
            make_store(tmp_path, database, FakeImageImporter()).inspect(package)
    finally:
        database.dispose()


def test_symlinks_and_duplicate_entries_are_rejected(tmp_path: Path) -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        symlink_package, *_ = package_bytes(tmp_path, mutate="symlink-case")
        info = zipfile.ZipInfo("assets/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(symlink_package, "a") as archive:
            archive.writestr(info, "../../outside")
        with pytest.raises(PackageInspectionError, match="link"):
            make_store(tmp_path, database, FakeImageImporter()).inspect(symlink_package)

        duplicate_package, *_ = package_bytes(tmp_path, mutate="duplicate-case")
        with pytest.warns(UserWarning):
            with zipfile.ZipFile(duplicate_package, "a") as archive:
                archive.writestr("plugin.json", b"{}")
        with pytest.raises(PackageInspectionError, match="duplicate"):
            make_store(tmp_path, database, FakeImageImporter()).inspect(duplicate_package)
    finally:
        database.dispose()


def test_archive_entry_size_and_compression_ratio_limits(tmp_path: Path) -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        package, *_ = package_bytes(tmp_path, mutate="limits")
        with zipfile.ZipFile(package, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("assets/bomb.txt", b"0" * 100_000)
        with pytest.raises(PackageInspectionError, match="compression ratio"):
            make_store(
                tmp_path,
                database,
                FakeImageImporter(),
                max_compression_ratio=5,
            ).inspect(package)
        with pytest.raises(PackageInspectionError, match="entries"):
            make_store(
                tmp_path,
                database,
                FakeImageImporter(),
                max_entries=3,
            ).inspect(package)
    finally:
        database.dispose()


def test_image_digest_and_host_compatibility_are_enforced(tmp_path: Path) -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        mismatched, *_ = package_bytes(
            tmp_path,
            manifest_image_digest="sha256:" + "0" * 64,
            mutate="digest-mismatch",
        )
        with pytest.raises(PackageInspectionError, match="image digest"):
            make_store(tmp_path, database, FakeImageImporter()).inspect(mismatched)

        incompatible, *_ = package_bytes(
            tmp_path,
            host_api=">=2.0 <3.0",
            mutate="incompatible-host",
        )
        with pytest.raises(PackageInspectionError, match="Host API"):
            make_store(tmp_path, database, FakeImageImporter()).inspect(incompatible)
    finally:
        database.dispose()
