from __future__ import annotations

import hashlib
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

try:
    from scripts.package_plugin import CommandRunner, build_plugin_package
except ModuleNotFoundError:
    from package_plugin import CommandRunner, build_plugin_package

ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = Path("plugin-sdk/python/matinier_plugin")


@dataclass(frozen=True)
class BuiltinPluginSource:
    source_id: str
    package_name: str

    @property
    def relative_root(self) -> Path:
        return Path("plugin-sdk/examples") / self.source_id

    def capture(self, *, project_root: Path = ROOT) -> dict[str, bytes]:
        return capture_plugin_sources(self.source_id, project_root=project_root)

    def digest(self, sources: Mapping[str, bytes]) -> str:
        return source_digest(sources)

    def write_snapshot(self, destination: Path, sources: Mapping[str, bytes]) -> Path:
        # Validate the entire closure before writing anything.
        own = self.relative_root
        for name in sources:
            path = _relative(name)
            static = path in {own / "Dockerfile", own / "plugin.json", own / "plugin.py"}
            python = path.suffix == ".py" and (
                path.is_relative_to(own / self.package_name) or path.is_relative_to(SDK_ROOT)
            )
            if "__pycache__" in path.parts or not (static or python):
                raise ValueError("built-in source is outside the declared closure")
        target = destination.absolute()
        if target.is_symlink():
            raise ValueError("plugin snapshot must not be a link")
        if target.exists():
            if not target.is_dir() or any(target.iterdir()):
                raise FileExistsError("plugin snapshot destination is not empty")
        else:
            target.mkdir(mode=0o700)
        root = target.resolve(strict=True)
        for name in sorted(sources):
            output = root / _relative(name)
            if not output.resolve().is_relative_to(root):
                raise ValueError("plugin source path escapes snapshot")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(sources[name])
        return root

    def build(self, *, output: Path, private_key_path: Path, image_tag: str,
              command_runner: CommandRunner = subprocess.run,
              build_context: Path | None = None) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        built = False

        def run(command, *, check):
            nonlocal built
            result = command_runner(command, check=check)
            if command[:2] == ["docker", "build"]:
                built = True
            return result

        try:
            if build_context is None:
                short = "course" if self.source_id == "course-organizer" else "meeting"
                temporary = tempfile.TemporaryDirectory(prefix=f"matinier-{short}-source-", dir=output.parent)
                context = self.write_snapshot(Path(temporary.name) / "context", self.capture())
            else:
                context = build_context.resolve(strict=True)
                self.capture(project_root=context)
            source = context / self.relative_root
            build_plugin_package(source_dir=source, build_context=context,
                dockerfile=source / "Dockerfile", manifest_path=source / "plugin.json",
                output=output, private_key_path=private_key_path, image_tag=image_tag,
                command_runner=run)
        finally:
            try:
                if built:
                    command_runner(["docker", "image", "rm", "--force", image_tag], check=False)
            finally:
                if temporary is not None:
                    temporary.cleanup()


BUILTIN_SOURCES = MappingProxyType({
    "course-organizer": BuiltinPluginSource("course-organizer", "course_organizer"),
    "meeting-assistant": BuiltinPluginSource("meeting-assistant", "meeting_assistant"),
})


def require_builtin_source(source_id: str) -> BuiltinPluginSource:
    try:
        return BUILTIN_SOURCES[source_id]
    except KeyError as error:
        raise LookupError("unknown built-in source") from error


def _require_real(path: Path, root: Path, *, directory: bool = False) -> None:
    valid_type = path.is_dir() if directory else path.is_file()
    if path.is_symlink() or not valid_type or not path.resolve(strict=True).is_relative_to(root):
        raise ValueError("plugin Docker COPY inputs must be real files/directories without links")
    for parent in path.relative_to(root).parents:
        if (root / parent).is_symlink():
            raise ValueError("plugin Docker COPY inputs must not contain links")


def capture_plugin_sources(source_id: str, *, project_root: Path = ROOT) -> dict[str, bytes]:
    source = require_builtin_source(source_id)
    root = project_root.resolve(strict=True)
    own = source.relative_root
    selected = [root / own / name for name in ("Dockerfile", "plugin.json", "plugin.py")]
    for relative in (own / source.package_name, SDK_ROOT):
        package = root / relative
        _require_real(package, root, directory=True)
        for path in sorted(package.rglob("*")):
            if path.is_symlink():
                raise ValueError("plugin Docker COPY inputs must not contain links")
            if "__pycache__" not in path.relative_to(package).parts and path.is_file() and path.suffix == ".py":
                selected.append(path)
    captured = {}
    for name, path in sorted((p.relative_to(root).as_posix(), p) for p in selected):
        _require_real(path, root)
        captured[name] = path.read_bytes()
    return captured


def _relative(name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or path.drive or ".." in path.parts:
        raise ValueError("plugin source path is invalid")
    return path


def source_digest(sources: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(sources):
        encoded = _relative(name).as_posix().encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(hashlib.sha256(sources[name]).digest())
    return "sha256:" + digest.hexdigest()
