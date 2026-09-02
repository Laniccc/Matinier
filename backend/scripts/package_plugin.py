from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


CommandRunner = Callable[..., subprocess.CompletedProcess[object]]


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def load_private_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("private key must be an unencrypted Ed25519 PEM key")
    return key


def build_plugin_package(
    *,
    source_dir: Path,
    build_context: Path,
    dockerfile: Path,
    manifest_path: Path,
    output: Path,
    private_key_path: Path,
    image_tag: str,
    command_runner: CommandRunner = subprocess.run,
) -> None:
    source = source_dir.resolve(strict=True)
    context = build_context.resolve(strict=True)
    dockerfile_path = dockerfile.resolve(strict=True)
    manifest_file = manifest_path.resolve(strict=True)
    destination = output.resolve()
    key_path = private_key_path.resolve(strict=True)
    if not source.is_dir() or not context.is_dir():
        raise ValueError("plugin source and build context must be directories")
    if not _within(source, context):
        raise ValueError("plugin source must be inside the explicit build context")
    if not _within(dockerfile_path, source) or not _within(manifest_file, source):
        raise ValueError("Dockerfile and manifest must be inside the plugin source")
    if _within(destination, source) or _within(destination, context):
        raise ValueError("package output must be outside source and build context")
    if _within(key_path, source) or _within(key_path, context):
        raise ValueError("private key must be outside source and build context")
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {destination}")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}", image_tag) is None:
        raise ValueError("invalid Docker image tag")
    manifest = json.loads(manifest_file.read_text("utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("publisher"), str):
        raise ValueError("plugin manifest is invalid")
    private_key = load_private_key(key_path)
    assets = _load_assets(source)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="matinier-plugin-package-") as raw:
        image_tar = Path(raw) / "image.tar"
        command_runner(
            [
                "docker",
                "build",
                "--file",
                str(dockerfile_path),
                "--tag",
                image_tag,
                str(context),
            ],
            check=True,
        )
        command_runner(
            [
                "docker",
                "image",
                "save",
                "--output",
                str(image_tar),
                image_tag,
            ],
            check=True,
        )
        if not image_tar.is_file():
            raise RuntimeError("Docker image save did not create an archive")
        image_digest = sha256_file(image_tar)
        manifest = dict(manifest)
        manifest["image_digest"] = image_digest
        package_assets_digest = _assets_digest(assets)
        material = (
            canonical_json(manifest)
            + b"\n"
            + image_digest.encode("ascii")
            + b"\n"
            + package_assets_digest.encode("ascii")
        )
        public_key = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        signature = {
            "schema_version": 1,
            "algorithm": "Ed25519",
            "publisher": manifest["publisher"],
            "public_key": base64.b64encode(public_key).decode("ascii"),
            "assets_digest": package_assets_digest,
            "signature": base64.b64encode(private_key.sign(material)).decode("ascii"),
        }
        entries = {
            "image.tar": image_tar.read_bytes(),
            "plugin.json": canonical_json(manifest),
            "signature.json": canonical_json(signature),
            **assets,
        }
        _write_deterministic_zip(destination, entries)


def _load_assets(source: Path) -> dict[str, bytes]:
    root = source / "assets"
    if not root.exists():
        return {}
    if root.is_symlink() or not root.is_dir():
        raise ValueError("plugin assets must be a real directory")
    output: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("plugin assets must not contain links")
        if path.is_dir():
            continue
        resolved = path.resolve(strict=True)
        if not _within(resolved, root.resolve()):
            raise ValueError("plugin asset escapes the assets directory")
        output["assets/" + path.relative_to(root).as_posix()] = path.read_bytes()
    return output


def _assets_digest(entries: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(entries):
        encoded_name = name.encode("utf-8")
        content_hash = hashlib.sha256(entries[name]).digest()
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(content_hash)
    return "sha256:" + digest.hexdigest()


def _write_deterministic_zip(path: Path, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(
        path,
        "x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, entries[name])


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


__all__ = ["build_plugin_package", "canonical_json", "load_private_key", "sha256_file"]
