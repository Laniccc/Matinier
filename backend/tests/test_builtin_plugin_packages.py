from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import stat
import subprocess
import threading
import zipfile
from functools import wraps
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

from app.plugins.builtin_packages import (
    BuiltinPackageBuildError,
    BuiltinPluginPackageService,
)
from app.plugins.builtins import BuiltinPluginRegistry
from app.plugins.signing import assets_digest, signed_material
from app.settings import PROJECT_ROOT, Settings
from scripts.builtin_plugin_sources import require_builtin_source


def async_test(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapped


def _settings(work_root: Path) -> Settings:
    return Settings(
        _env_file=None,
        livekit_url="ws://localhost:7880",
        livekit_api_key="key",
        livekit_api_secret="secret",
        livekit_room_name="room",
        plugin_builtin_build_enabled=True,
        plugin_builtin_work_dir=work_root,
    )


class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool]] = []

    def __call__(
        self,
        args: list[object],
        *,
        check: bool,
    ) -> subprocess.CompletedProcess[object]:
        command = [str(item) for item in args]
        self.calls.append((command, check))
        if command[:3] == ["docker", "image", "save"]:
            output = Path(command[command.index("--output") + 1])
            output.write_bytes(b"fake course image")
        return subprocess.CompletedProcess(command, 0)


class FakePackageBuilder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail = False
        self.started: threading.Event | None = None
        self.release: threading.Event | None = None

    def __call__(self, **kwargs: object) -> None:
        self.calls.append(dict(kwargs))
        if self.started is not None:
            Path(kwargs["output"]).write_bytes(b"builder is still writing")
            self.started.set()
        if self.release is not None:
            assert self.release.wait(timeout=5)
            Path(kwargs["output"]).unlink()
        if self.fail:
            raise RuntimeError(
                f"builder leaked {kwargs['private_key_path']}"
            )
        source_id = next(Path(kwargs["build_context"]).glob("plugin-sdk/examples/*/plugin.json")).parent.name
        require_builtin_source(source_id).build(
            output=Path(kwargs["output"]),
            private_key_path=Path(kwargs["private_key_path"]),
            image_tag=str(kwargs["image_tag"]),
            build_context=Path(kwargs["build_context"]),
            command_runner=FakeDocker(),
        )


def _service(work_root: Path, builder: FakePackageBuilder) -> BuiltinPluginPackageService:
    settings = _settings(work_root)
    return BuiltinPluginPackageService(
        settings=settings,
        registry=BuiltinPluginRegistry(settings),
        builder=builder,
    )


def _assert_valid_course_package(path: Path, plugin_id="com.matinier.course-organizer") -> None:
    assert path.is_file() and path.stat().st_size > 0
    with zipfile.ZipFile(path) as archive:
        assert {"image.tar", "plugin.json", "signature.json"} <= set(
            archive.namelist()
        )
        manifest = json.loads(archive.read("plugin.json"))
        image = archive.read("image.tar")
    assert manifest["id"] == plugin_id
    assert manifest["version"] == ("1.0.3" if plugin_id.endswith("course-organizer") else "1.0.0")
    assert manifest["image_digest"] == (
        "sha256:" + hashlib.sha256(image).hexdigest()
    )


@async_test
async def test_prepare_generates_one_reused_external_private_key_and_valid_cache(
    tmp_path: Path,
) -> None:
    work_root = (tmp_path / "builtin-work").resolve()
    builder = FakePackageBuilder()
    service = _service(work_root, builder)

    first = await service.prepare("com.matinier.course-organizer")
    second = await service.prepare("com.matinier.course-organizer")

    assert first == second
    assert len(builder.calls) == 1
    _assert_valid_course_package(first)
    assert first.resolve().is_relative_to(work_root)
    assert not first.resolve().is_relative_to(PROJECT_ROOT.resolve())

    key_dir = work_root / "keys"
    keys = list(key_dir.iterdir())
    assert len(keys) == 1
    key_path = keys[0]
    assert key_path.is_file() and not key_path.is_symlink()
    loaded = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    assert isinstance(loaded, Ed25519PrivateKey)
    if os.name != "nt":
        assert stat.S_IMODE(key_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert str(key_path) not in str(first)


@pytest.mark.parametrize("plugin_id", ["com.matinier.course-organizer", "com.matinier.meeting-assistant"])
@async_test
async def test_prepare_is_singleflight_across_service_instances(
    tmp_path: Path,
    plugin_id: str,
) -> None:
    work_root = (tmp_path / "builtin-work").resolve()
    builder = FakePackageBuilder()
    first_service = _service(work_root, builder)
    second_service = _service(work_root, builder)

    first, second = await asyncio.gather(
        first_service.prepare(plugin_id),
        second_service.prepare(plugin_id),
    )

    assert first == second
    assert len(builder.calls) == 1


@pytest.mark.parametrize("plugin_id", ["com.matinier.course-organizer", "com.matinier.meeting-assistant"])
@async_test
async def test_cancelled_waiters_do_not_cancel_build_or_cleanup_live_partial(
    tmp_path: Path,
    plugin_id: str,
) -> None:
    work_root = (tmp_path / "builtin-work").resolve()
    builder = FakePackageBuilder()
    builder.started = threading.Event()
    builder.release = threading.Event()
    first_service = _service(work_root, builder)
    second_service = _service(work_root, builder)

    first = asyncio.create_task(
        first_service.prepare(plugin_id)
    )
    await asyncio.to_thread(builder.started.wait, 5)
    joined = asyncio.Event()
    async def join_second_waiter():
        joined.set()
        return await second_service.prepare(plugin_id)
    second = asyncio.create_task(join_second_waiter())
    await joined.wait()
    first.cancel()
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(asyncio.CancelledError):
        await second

    assert len(builder.calls) == 1
    assert list(work_root.rglob("*.partial-*.plugin.zip"))
    builder.release.set()

    cached = await first_service.prepare(plugin_id)
    _assert_valid_course_package(cached, plugin_id)
    assert len(builder.calls) == 1


@async_test
async def test_corrupt_cache_is_rebuilt_and_verified(
    tmp_path: Path,
) -> None:
    work_root = (tmp_path / "builtin-work").resolve()
    builder = FakePackageBuilder()
    service = _service(work_root, builder)
    cached = await service.prepare("com.matinier.course-organizer")

    cached.write_bytes(b"not a package")
    rebuilt = await service.prepare("com.matinier.course-organizer")

    assert rebuilt == cached
    assert len(builder.calls) == 2
    _assert_valid_course_package(rebuilt)


@pytest.mark.parametrize(
    "corruption",
    ("manifest_id", "manifest_version", "image_digest", "signature"),
)
@pytest.mark.parametrize("plugin_id", ["com.matinier.course-organizer", "com.matinier.meeting-assistant"])
@async_test
async def test_wrong_manifest_digest_or_signature_cache_is_rebuilt(
    tmp_path: Path,
    plugin_id: str,
    corruption: str,
) -> None:
    work_root = (tmp_path / "builtin-work").resolve()
    builder = FakePackageBuilder()
    service = _service(work_root, builder)
    cached = await service.prepare(plugin_id)

    with zipfile.ZipFile(cached) as source:
        entries = {name: source.read(name) for name in source.namelist()}
    if corruption == "signature":
        signature = json.loads(entries["signature.json"])
        signature["signature"] = base64.b64encode(b"\0" * 64).decode("ascii")
        entries["signature.json"] = json.dumps(signature).encode()
    else:
        manifest = json.loads(entries["plugin.json"])
        if corruption == "manifest_id":
            manifest["id"] = "com.attacker.course-organizer"
        elif corruption == "manifest_version":
            manifest["version"] = "9.9.9"
        else:
            manifest["image_digest"] = "sha256:" + "0" * 64
        entries["plugin.json"] = json.dumps(manifest).encode()
    cached.unlink()
    with zipfile.ZipFile(cached, "x") as target:
        for name, content in entries.items():
            target.writestr(name, content)

    await service.prepare(plugin_id)

    assert len(builder.calls) == 2
    _assert_valid_course_package(cached, plugin_id)


@async_test
async def test_build_failure_preserves_previous_valid_digest_cache_and_hides_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts import builtin_plugin_sources as package_module

    work_root = (tmp_path / "builtin-work").resolve()
    builder = FakePackageBuilder()
    service = _service(work_root, builder)
    first = await service.prepare("com.matinier.course-organizer")
    first_bytes = first.read_bytes()

    original_capture = package_module.capture_plugin_sources
    captured = original_capture("course-organizer")
    changed = dict(captured)
    changed["plugin-sdk/examples/course-organizer/plugin.py"] += b"\n# changed\n"
    monkeypatch.setattr(
        package_module,
        "capture_plugin_sources",
        lambda source_id, **kwargs: changed,
    )
    builder.fail = True

    with pytest.raises(BuiltinPackageBuildError) as error:
        await service.prepare("com.matinier.course-organizer")

    assert str(work_root) not in str(error.value)
    assert "private" not in str(error.value).casefold()
    assert first.read_bytes() == first_bytes
    assert not list(work_root.rglob("*.partial-*.plugin.zip"))


@async_test
async def test_key_symlink_and_wrong_pem_type_are_rejected_without_path_leak(
    tmp_path: Path,
) -> None:
    work_root = (tmp_path / "builtin-work").resolve()
    key_dir = work_root / "keys"
    key_dir.mkdir(parents=True)
    outside = tmp_path / "outside-key.pem"
    outside.write_bytes(b"outside")
    key_path = key_dir / "builtin-signing-key.pem"
    try:
        key_path.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    service = _service(work_root, FakePackageBuilder())

    with pytest.raises(BuiltinPackageBuildError) as error:
        await service.prepare("com.matinier.course-organizer")
    assert str(key_path) not in str(error.value)
    assert outside.read_bytes() == b"outside"



@async_test
async def test_non_ed25519_private_key_is_rejected(tmp_path: Path) -> None:
    work_root = (tmp_path / "builtin-work").resolve()
    key_dir = work_root / "keys"
    key_dir.mkdir(parents=True)
    key_path = key_dir / "builtin-signing-key.pem"
    rsa_key = generate_private_key(public_exponent=65537, key_size=2048)
    key_path.write_bytes(
        rsa_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    with pytest.raises(BuiltinPackageBuildError):
        await _service(work_root, FakePackageBuilder()).prepare(
            "com.matinier.course-organizer"
        )


@async_test
async def test_non_directory_cache_component_is_rejected(tmp_path: Path) -> None:
    work_root = (tmp_path / "builtin-work").resolve()
    work_root.mkdir()
    (work_root / "cache").write_bytes(b"not a directory")

    with pytest.raises(BuiltinPackageBuildError):
        await _service(work_root, FakePackageBuilder()).prepare(
            "com.matinier.course-organizer"
        )


@async_test
async def test_cache_symlink_is_rejected_and_never_overwrites_target(
    tmp_path: Path,
) -> None:
    work_root = (tmp_path / "builtin-work").resolve()
    cache_root = work_root / "cache"
    cache_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    plugin_cache = cache_root / "com.matinier.course-organizer"
    try:
        plugin_cache.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(BuiltinPackageBuildError):
        await _service(work_root, FakePackageBuilder()).prepare(
            "com.matinier.course-organizer"
        )
    assert list(outside.iterdir()) == []


@async_test
async def test_each_real_build_receives_unique_image_tag_and_new_empty_partial(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts import builtin_plugin_sources as package_module

    work_root = (tmp_path / "builtin-work").resolve()
    builder = FakePackageBuilder()
    service = _service(work_root, builder)
    await service.prepare("com.matinier.course-organizer")
    initial = package_module.capture_plugin_sources("course-organizer")
    changed = dict(initial)
    changed["plugin-sdk/examples/course-organizer/plugin.py"] += b"\n# v2\n"
    monkeypatch.setattr(
        package_module,
        "capture_plugin_sources",
        lambda source_id, **kwargs: changed,
    )
    await service.prepare("com.matinier.course-organizer")

    assert len(builder.calls) == 2
    tags = [str(call["image_tag"]) for call in builder.calls]
    assert len(set(tags)) == 2
    assert all("com.matinier.course-organizer" not in tag for tag in tags)
    outputs = [Path(call["output"]) for call in builder.calls]
    assert len(set(outputs)) == 2
    assert all(".partial-" in output.name for output in outputs)


def test_signature_test_helper_is_well_formed() -> None:
    """Keep security imports exercised independently of service internals."""

    key = Ed25519PrivateKey.generate()
    manifest = {"publisher": "Matinier", "image_digest": "sha256:" + "0" * 64}
    material = signed_material(
        manifest,
        manifest["image_digest"],
        assets_digest({}),
    )
    signature = base64.b64encode(key.sign(material))
    assert signature
