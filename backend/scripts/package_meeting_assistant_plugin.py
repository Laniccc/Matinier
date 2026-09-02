from __future__ import annotations

import argparse
import subprocess
import uuid
from pathlib import Path

try:
    from scripts.builtin_plugin_sources import require_builtin_source
except ModuleNotFoundError:
    from builtin_plugin_sources import require_builtin_source


def package_meeting_plugin(*, output: Path, private_key_path: Path, image_tag: str,
                           command_runner=subprocess.run, build_context: Path | None = None) -> None:
    require_builtin_source("meeting-assistant").build(output=output, private_key_path=private_key_path,
        image_tag=image_tag, command_runner=command_runner, build_context=build_context)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build, digest, sign, and package the meeting assistant plugin.")
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--image-tag")
    args = parser.parse_args()
    package_meeting_plugin(output=args.output, private_key_path=args.private_key,
        image_tag=args.image_tag or f"matinier-meeting-assistant-plugin:build-{uuid.uuid4().hex}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
