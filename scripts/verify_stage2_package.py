from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.packages import (  # noqa: E402
    PackageBuildError,
    PackageBuilder,
    PackageValidator,
    PackageZipExporter,
)
from app.packages.exporter import verify_package_zip  # noqa: E402
from app.persistence.database import Database  # noqa: E402
from app.persistence.sessions import SessionRepository  # noqa: E402
from app.settings import get_settings  # noqa: E402


def verify(
    *,
    session_id: str | None,
    output_path: Path | None,
) -> dict[str, object]:
    settings = get_settings()
    database = Database(settings.database_url)
    try:
        with database.session() as db_session:
            candidates = (
                [SessionRepository(db_session).get_required(session_id)]
                if session_id is not None
                else SessionRepository(db_session).list_newest_first()
            )
            package = None
            selected_session_id = None
            for candidate in candidates:
                try:
                    package = PackageBuilder(db_session).build_baseline(
                        candidate.id
                    )
                except PackageBuildError:
                    db_session.rollback()
                    continue
                selected_session_id = candidate.id
                break
            if package is None or selected_session_id is None:
                raise RuntimeError(
                    "No completed Session or Session with Final captions was found"
                )
            validation = PackageValidator().validate(package)
            if not validation.valid:
                raise RuntimeError("Package validation failed: " + "; ".join(validation.errors))
            zip_content = PackageZipExporter().export(package)
            zip_valid, zip_errors = verify_package_zip(zip_content)
            if not zip_valid:
                raise RuntimeError("ZIP validation failed: " + "; ".join(zip_errors))
            db_session.commit()

        if output_path is not None:
            resolved_output = output_path.resolve()
            resolved_output.parent.mkdir(parents=True, exist_ok=True)
            resolved_output.write_bytes(zip_content)
            output_value: str | None = str(resolved_output)
        else:
            output_value = None
        return {
            "status": "ok",
            "session_id": selected_session_id,
            "package_id": package.package_id,
            "package_version": package.package_version,
            "content_hash": package.content_hash,
            "document_count": len(package.documents),
            "zip_bytes": len(zip_content),
            "checksums_valid": True,
            "output_path": output_value,
        }
    finally:
        database.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build and verify one Stage 2A canonical transcript package.",
    )
    parser.add_argument("--session-id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = verify(
            session_id=args.session_id,
            output_path=args.output,
        )
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "message": str(error),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
