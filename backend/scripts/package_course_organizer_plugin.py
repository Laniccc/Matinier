from __future__ import annotations

import argparse
import subprocess
import uuid
from pathlib import Path

try:
    from scripts.builtin_plugin_sources import ROOT, require_builtin_source
except ModuleNotFoundError:
    from builtin_plugin_sources import ROOT, require_builtin_source

SOURCE = ROOT / "plugin-sdk/examples/course-organizer"


def capture_course_plugin_sources(*, project_root: Path = ROOT) -> dict[str, bytes]:
    return require_builtin_source("course-organizer").capture(project_root=project_root)


def course_plugin_source_digest(sources) -> str:
    return require_builtin_source("course-organizer").digest(sources)


def write_course_plugin_snapshot(destination: Path, sources) -> Path:
    return require_builtin_source("course-organizer").write_snapshot(destination, sources)


def package_course_plugin(*, output: Path, private_key_path: Path, image_tag: str,
                          command_runner=subprocess.run, build_context: Path | None = None) -> None:
    require_builtin_source("course-organizer").build(output=output, private_key_path=private_key_path,
        image_tag=image_tag, command_runner=command_runner, build_context=build_context)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build, digest, sign, and package the course organizer plugin.")
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--image-tag")
    args = parser.parse_args()
    package_course_plugin(output=args.output, private_key_path=args.private_key,
        image_tag=args.image_tag or f"matinier-course-organizer-plugin:build-{uuid.uuid4().hex}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["capture_course_plugin_sources", "course_plugin_source_digest", "package_course_plugin", "write_course_plugin_snapshot"]
