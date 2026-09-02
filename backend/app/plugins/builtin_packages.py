from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import stat
import threading
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.plugins.builtins import BuiltinPluginDescriptor, BuiltinPluginRegistry
from app.plugins.contracts import PluginManifest
from app.plugins.signing import (
    SignatureEnvelope,
    assets_digest,
    decode_public_key,
    verify_signature,
)
from app.settings import PROJECT_ROOT, Settings
from scripts.builtin_plugin_sources import require_builtin_source


PackageBuilder = Callable[..., None]


class BuiltinPackageBuildDisabledError(RuntimeError):
    """Dynamic built-in packaging is unavailable in this Host configuration."""


class BuiltinPackageBuildError(RuntimeError):
    """A deliberately opaque built-in packaging failure."""


# Singleflight is process-local by design. Deployment remains single-instance;
# an eventual multi-instance Host needs an inter-process/distributed build lock.
_TASKS_GUARD = threading.Lock()
_INFLIGHT: dict[tuple[str, str], asyncio.Task[Path]] = {}
_BUILD_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_KEY_LOCKS: dict[str, threading.Lock] = {}


class BuiltinPluginPackageService:
    """Build signed allowlisted packages into a server-owned external cache."""

    def __init__(
        self,
        *,
        settings: Settings,
        registry: BuiltinPluginRegistry,
        builder: PackageBuilder | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._builder = builder
        self._work_root = settings.plugin_builtin_work_dir.resolve()
        project_root = PROJECT_ROOT.resolve()
        if (
            not self._work_root.is_absolute()
            or self._work_root.is_relative_to(project_root)
        ):
            raise ValueError("built-in work root must be external")

    async def prepare(self, plugin_id: str) -> Path:
        descriptor = self._registry.require(plugin_id)
        if not descriptor.dynamic_build_available:
            raise BuiltinPackageBuildDisabledError(
                "built-in package preparation is disabled"
            )

        key = (os.path.normcase(str(self._work_root)), descriptor.plugin_id)
        loop = asyncio.get_running_loop()
        with _TASKS_GUARD:
            task = _INFLIGHT.get(key)
            if task is None or task.done():
                build_lock = _BUILD_LOCKS.setdefault(key, threading.Lock())
                task = loop.create_task(
                    self._run_shared_build(descriptor, build_lock),
                    name=f"builtin-package:{descriptor.plugin_id}",
                )
                _INFLIGHT[key] = task
                task.add_done_callback(
                    lambda completed, task_key=key: _discard_inflight(
                        task_key,
                        completed,
                    )
                )
        # Cancelling any/all request waiters must not cancel the shared task.
        return await asyncio.shield(task)

    async def _run_shared_build(
        self,
        descriptor: BuiltinPluginDescriptor,
        build_lock: threading.Lock,
    ) -> Path:
        try:
            # The thread owns locking, partial cleanup, and snapshot cleanup. Even
            # if an outer coroutine is cancelled, none of those happen while the
            # synchronous builder can still be writing.
            return await asyncio.to_thread(
                self._prepare_sync,
                descriptor,
                build_lock,
            )
        except BuiltinPackageBuildDisabledError:
            raise
        except Exception:
            raise BuiltinPackageBuildError(
                "built-in package preparation failed"
            ) from None

    def _prepare_sync(
        self,
        descriptor: BuiltinPluginDescriptor,
        build_lock: threading.Lock,
    ) -> Path:
        with build_lock:
            root = _secure_directory(self._work_root, self._work_root, mode=0o700)
            key_dir = _secure_directory(root / "keys", root, mode=0o700)
            cache_root = _secure_directory(root / "cache", root, mode=0o700)
            snapshot_root = _secure_directory(root / "snapshots", root, mode=0o700)
            private_key_path = key_dir / "builtin-signing-key.pem"
            # Different plugins build concurrently, but cannot read a half-written shared key.
            with _TASKS_GUARD:
                key_lock = _KEY_LOCKS.setdefault(os.path.normcase(str(root)), threading.Lock())
            with key_lock:
                private_key = _load_or_create_private_key(private_key_path, root)

            source = require_builtin_source(descriptor.source_id)
            sources = source.capture()
            source_digest = source.digest(sources)
            digest_hex = source_digest.removeprefix("sha256:")
            plugin_cache = _secure_directory(
                cache_root / descriptor.plugin_id,
                root,
                mode=0o700,
            )
            cached = plugin_cache / f"{digest_hex}.plugin.zip"
            _require_contained_path(cached, root)
            if _valid_cached_package(
                cached,
                descriptor=descriptor,
                private_key=private_key,
                max_bytes=self._settings.plugin_package_max_compressed_bytes,
                max_uncompressed_bytes=(
                    self._settings.plugin_package_max_uncompressed_bytes
                ),
            ):
                return cached

            nonce = uuid.uuid4().hex
            partial = plugin_cache / f"{digest_hex}.partial-{nonce}.plugin.zip"
            snapshot = snapshot_root / f"{descriptor.source_id}-{nonce}"
            _require_new_path(partial, root)
            _require_new_path(snapshot, root)
            image_tag = (
                f"matinier-{descriptor.source_id}-plugin:"
                f"builtin-{digest_hex[:12]}-{nonce}"
            )
            try:
                source.write_snapshot(snapshot, sources)
                (self._builder or source.build)(
                    output=partial,
                    private_key_path=private_key_path,
                    image_tag=image_tag,
                    build_context=snapshot,
                )
                if not _valid_cached_package(
                    partial,
                    descriptor=descriptor,
                    private_key=private_key,
                    max_bytes=self._settings.plugin_package_max_compressed_bytes,
                    max_uncompressed_bytes=(
                        self._settings.plugin_package_max_uncompressed_bytes
                    ),
                ):
                    raise ValueError("built-in package validation failed")
                _assert_regular_or_missing(cached, root)
                partial.replace(cached)
                return cached
            finally:
                # This finally executes in the builder thread, never in a
                # cancelled request waiter.
                try:
                    _unlink_regular_file(partial, root)
                finally:
                    _remove_snapshot(snapshot, snapshot_root)


def _discard_inflight(
    key: tuple[str, str],
    completed: asyncio.Task[Path],
) -> None:
    if not completed.cancelled():
        # Retrieve failures even when every request waiter was cancelled.
        completed.exception()
    with _TASKS_GUARD:
        if _INFLIGHT.get(key) is completed:
            _INFLIGHT.pop(key, None)


def _secure_directory(path: Path, root: Path, *, mode: int) -> Path:
    root = root.absolute()
    path = path.absolute()
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ValueError("built-in work path escapes its root") from error

    if root.is_symlink():
        raise ValueError("built-in work root must not be a link")
    if root.exists():
        if not root.is_dir():
            raise ValueError("built-in work root is not a directory")
    else:
        root.mkdir(parents=True, mode=mode, exist_ok=True)
    resolved_root = root.resolve(strict=True)
    cursor = resolved_root
    for component in relative.parts:
        cursor = cursor / component
        if cursor.is_symlink():
            raise ValueError("built-in work directories must not be links")
        if cursor.exists():
            if not cursor.is_dir():
                raise ValueError("built-in work directory is not a directory")
        else:
            cursor.mkdir(mode=mode, exist_ok=True)
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(resolved_root):
        raise ValueError("built-in work directory escapes its root")
    try:
        resolved.chmod(mode)
    except OSError:
        if os.name != "nt":
            raise
    return resolved


def _load_or_create_private_key(
    path: Path,
    root: Path,
) -> Ed25519PrivateKey:
    _require_contained_path(path, root)
    if not path.exists() and not path.is_symlink():
        private_key = Ed25519PrivateKey.generate()
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            pass
        else:
            write_failed = False
            try:
                view = memoryview(pem)
                while view:
                    written = os.write(descriptor, view)
                    view = view[written:]
                os.fsync(descriptor)
            except BaseException:
                write_failed = True
                raise
            finally:
                os.close(descriptor)
                if write_failed and path.exists() and not path.is_symlink():
                    path.unlink()
    _assert_regular_file(path, root)
    try:
        path.chmod(0o600)
    except OSError:
        if os.name != "nt":
            raise
    loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(loaded, Ed25519PrivateKey):
        raise ValueError("built-in signing key has the wrong type")
    return loaded


def _valid_cached_package(
    path: Path,
    *,
    descriptor: BuiltinPluginDescriptor,
    private_key: Ed25519PrivateKey,
    max_bytes: int,
    max_uncompressed_bytes: int,
) -> bool:
    try:
        _assert_regular_file(path, path.parent.parent.parent)
        if path.stat().st_size <= 0 or path.stat().st_size > max_bytes:
            return False
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if sum(info.file_size for info in infos) > max_uncompressed_bytes:
                return False
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                return False
            required = {"image.tar", "plugin.json", "signature.json"}
            if not required.issubset(names):
                return False
            if any(
                name not in required and not name.startswith("assets/")
                for name in names
            ):
                return False
            if any(
                stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK
                for info in infos
            ):
                return False
            image = archive.read("image.tar")
            manifest_raw = json.loads(archive.read("plugin.json"))
            signature = SignatureEnvelope.model_validate_json(
                archive.read("signature.json")
            )
            asset_entries = {
                name: archive.read(name)
                for name in names
                if name.startswith("assets/")
            }
        manifest = PluginManifest.model_validate(manifest_raw)
        image_digest = "sha256:" + hashlib.sha256(image).hexdigest()
        if (
            manifest.id != descriptor.plugin_id
            or manifest.version != descriptor.version
            or manifest.permissions != descriptor.permissions
            or manifest.image_digest != image_digest
            or signature.publisher != manifest.publisher
        ):
            return False
        package_assets_digest = assets_digest(asset_entries)
        if signature.assets_digest != package_assets_digest:
            return False
        expected_public_key = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        if decode_public_key(signature.public_key) != expected_public_key:
            return False
        verify_signature(
            signature,
            manifest=manifest_raw,
            image_digest=image_digest,
            package_assets_digest=package_assets_digest,
        )
        return True
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile):
        return False


def _require_contained_path(path: Path, root: Path) -> None:
    absolute = path.absolute()
    try:
        absolute.relative_to(root.resolve(strict=True))
    except ValueError as error:
        raise ValueError("built-in work path escapes its root") from error
    parent = absolute.parent.resolve(strict=True)
    if not parent.is_relative_to(root.resolve(strict=True)):
        raise ValueError("built-in work path escapes its root")


def _require_new_path(path: Path, root: Path) -> None:
    _require_contained_path(path, root)
    if path.exists() or path.is_symlink():
        raise FileExistsError("built-in temporary path already exists")


def _assert_regular_file(path: Path, root: Path) -> None:
    _require_contained_path(path, root)
    if path.is_symlink() or not path.is_file():
        raise ValueError("built-in work file must be a regular file")


def _assert_regular_or_missing(path: Path, root: Path) -> None:
    _require_contained_path(path, root)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("built-in cache target must be a regular file")


def _unlink_regular_file(path: Path, root: Path) -> None:
    _require_contained_path(path, root)
    if path.is_symlink():
        raise ValueError("built-in partial path became a link")
    if path.exists():
        if not path.is_file():
            raise ValueError("built-in partial path is not a regular file")
        path.unlink()


def _remove_snapshot(path: Path, snapshot_root: Path) -> None:
    _require_contained_path(path, snapshot_root)
    if path.is_symlink():
        raise ValueError("built-in snapshot path became a link")
    if path.exists():
        if not path.is_dir():
            raise ValueError("built-in snapshot path is not a directory")
        shutil.rmtree(path)


__all__ = [
    "BuiltinPackageBuildDisabledError",
    "BuiltinPackageBuildError",
    "BuiltinPluginPackageService",
]
