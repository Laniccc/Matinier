from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

try:
    from scripts.package_plugin import CommandRunner, build_plugin_package
except ModuleNotFoundError:
    from package_plugin import CommandRunner, build_plugin_package


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "plugin-sdk" / "examples" / "diagnostic"


def package_plugin(
    *,
    output: Path,
    private_key_path: Path,
    image_tag: str,
    command_runner: CommandRunner = subprocess.run,
) -> None:
    build_plugin_package(
        source_dir=SOURCE,
        build_context=SOURCE,
        dockerfile=SOURCE / "Dockerfile",
        manifest_path=SOURCE / "plugin.json",
        output=output,
        private_key_path=private_key_path,
        image_tag=image_tag,
        command_runner=command_runner,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build, digest, sign, and package the stdlib diagnostic plugin. "
            "The output and key must be outside its build context."
        )
    )
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--image-tag",
        default="matinier-diagnostic-plugin:1.0.0",
    )
    args = parser.parse_args()
    package_plugin(
        output=args.output,
        private_key_path=args.private_key,
        image_tag=args.image_tag,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
