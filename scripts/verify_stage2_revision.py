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
    PackageRepository,
    PackageValidator,
    PackageZipExporter,
)
from app.packages.exporter import verify_package_zip  # noqa: E402
from app.persistence.database import Database  # noqa: E402
from app.persistence.segments import SegmentRepository  # noqa: E402
from app.persistence.sessions import SessionRepository  # noqa: E402
from app.revisions import RevisionService, TranscriptRevisionContent  # noqa: E402
from app.settings import get_settings  # noqa: E402


def _stage1_snapshot(db_session, session_id: str) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            item.id,
            item.segment_id,
            item.revision,
            item.raw_text,
            item.display_text,
            item.audio_start_ms,
            item.audio_end_ms,
            item.status,
        )
        for item in SegmentRepository(db_session).list_final(session_id)
    )


def _select_base_package(db_session, session_id: str | None):
    sessions = SessionRepository(db_session)
    candidates = (
        [sessions.get_required(session_id)]
        if session_id is not None
        else sessions.list_newest_first()
    )
    packages = PackageRepository(db_session)
    for candidate in candidates:
        records = packages.list_for_session(candidate.id)
        for record in records:
            try:
                package = packages.load(record.id)
            except ValueError:
                continue
            if package.status in {"frozen", "superseded"} and any(
                document.document_kind in {"source_raw", "source_approved"}
                and bool(document.content.items)
                for document in package.documents
                if hasattr(document.content, "items")
            ):
                return candidate.id, package
        try:
            package = PackageBuilder(db_session).build_baseline(candidate.id)
        except PackageBuildError:
            db_session.rollback()
            continue
        if any(
            document.document_kind == "source_raw"
            and bool(document.content.items)
            for document in package.documents
            if hasattr(document.content, "items")
        ):
            return candidate.id, package
        db_session.rollback()
    raise RuntimeError("No Package with source items could be found or built")


def verify(
    *,
    session_id: str | None,
    output_path: Path | None,
) -> dict[str, object]:
    settings = get_settings()
    database = Database(settings.database_url)
    try:
        with database.session() as db_session:
            selected_session_id, base_package = _select_base_package(
                db_session,
                session_id,
            )
            stage1_before = _stage1_snapshot(db_session, selected_session_id)
            base_hash = base_package.content_hash
            base_document_hashes = tuple(
                document.content_hash for document in base_package.documents
            )

            service = RevisionService(db_session)
            first = service.create_from_package(
                base_package.package_id,
                change_summary="Stage 2B verifier initial snapshot",
            )
            second = service.create_version(
                first.revision_id,
                content=TranscriptRevisionContent(items=first.content.items),
                change_summary="Stage 2B verifier immutable copy",
            )
            approved = service.approve(second.revision_id)
            package = PackageBuilder(db_session).build_from_revision(approved)
            validation = PackageValidator().validate(package)
            if not validation.valid:
                raise RuntimeError(
                    "Package validation failed: " + "; ".join(validation.errors)
                )
            zip_content = PackageZipExporter().export(package)
            zip_valid, zip_errors = verify_package_zip(zip_content)
            if not zip_valid:
                raise RuntimeError(
                    "ZIP validation failed: " + "; ".join(zip_errors)
                )

            reloaded_base = PackageRepository(db_session).load(
                base_package.package_id
            )
            if reloaded_base.content_hash != base_hash:
                raise RuntimeError("Base Package content hash changed")
            if tuple(
                document.content_hash for document in reloaded_base.documents
            ) != base_document_hashes:
                raise RuntimeError("Base Package document hash changed")
            stage1_after = _stage1_snapshot(db_session, selected_session_id)
            if stage1_after != stage1_before:
                raise RuntimeError("Stage 1 Final evidence changed")
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
            "base_package_id": base_package.package_id,
            "base_package_hash_unchanged": True,
            "revision_versions": [first.version, second.version],
            "approved_revision_id": approved.revision_id,
            "package_id": package.package_id,
            "package_version": package.package_version,
            "package_content_hash": package.content_hash,
            "source_revision_id": package.source_revision_id,
            "stage1_final_count": len(stage1_after),
            "stage1_final_unchanged": True,
            "checksums_valid": True,
            "zip_bytes": len(zip_content),
            "output_path": output_value,
        }
    finally:
        database.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create immutable Revision versions, approve one, and verify a "
            "Stage 2B Package without changing Stage 1 Final evidence."
        ),
    )
    parser.add_argument("--session-id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = verify(session_id=args.session_id, output_path=args.output)
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
