from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.artifacts.exporter import ArtifactExporter  # noqa: E402
from app.artifacts.models import DerivedArtifact  # noqa: E402
from app.artifacts.repository import ArtifactRepository  # noqa: E402
from app.export.models import format_timestamp  # noqa: E402
from app.packages import PackageBuildError, PackageBuilder, PackageRepository  # noqa: E402
from app.persistence.database import Database  # noqa: E402
from app.persistence.segments import SegmentRepository  # noqa: E402
from app.persistence.sessions import SessionRepository  # noqa: E402
from app.processing.chapter_outline import ChapterOutlineWorkflow  # noqa: E402
from app.processing.clean_script import CleanScriptWorkflow  # noqa: E402
from app.processing.contracts import (  # noqa: E402
    EvidenceValidator,
    PackageReader,
    ProcessorRegistry,
)
from app.processing.repository import ProcessingJobRepository  # noqa: E402
from app.processing.refined_translation import (  # noqa: E402
    RefinedTranslationWorkflow,
)
from app.processing.runner import ProcessingJobRunner  # noqa: E402
from app.processing.summary import SummaryWorkflow  # noqa: E402
from app.processing.timeline_fact_review import (  # noqa: E402
    TimelineFactReviewWorkflow,
)
from app.settings import get_settings  # noqa: E402
from app.text_processing.provider import (  # noqa: E402
    StructuredCompletionRequest,
    StructuredCompletionResult,
)


class LocalStructuredProvider:
    """Credential-free provider for the Stage 2C through 2E workflows."""

    provider_name = "fake"
    model = "stage2e-local-verifier-v1"

    def __init__(self) -> None:
        self.request_count = 0
        self.live_reference_item_count = 0

    async def complete_structured(
        self,
        request: StructuredCompletionRequest,
    ) -> StructuredCompletionResult:
        self.request_count += 1
        claims = request.input_payload.get("claims")
        if isinstance(claims, list):
            package_items = request.input_payload["package_items"]
            evidence_item_id = package_items[0]["item_id"]
            return StructuredCompletionResult(
                content=json.dumps(
                    {
                        "reviews": [
                            {
                                "claim_id": claim["claim_id"],
                                "status": "supported",
                                "evidence_item_ids": [evidence_item_id],
                                "explanation": (
                                    "Supported by credential-free Package evidence"
                                ),
                            }
                            for claim in claims
                        ],
                        "warnings": [],
                    },
                    ensure_ascii=False,
                ),
                finish_reason="stop",
            )
        items = request.input_payload["items"]
        target_language = request.input_payload.get("target_language")
        if isinstance(target_language, str):
            live_reference = request.input_payload.get(
                "live_translation_reference",
                [],
            )
            if isinstance(live_reference, list):
                self.live_reference_item_count += len(live_reference)
            translated_text = " ".join(item["text"] for item in items)
            glossary = request.input_payload.get("glossary", {})
            if isinstance(glossary, dict):
                for source, target in glossary.items():
                    translated_text = translated_text.replace(source, target)
            return StructuredCompletionResult(
                content=json.dumps(
                    {
                        "sections": [
                            {
                                "source_item_ids": [
                                    item["item_id"] for item in items
                                ],
                                "translated_text": (
                                    f"[{target_language}] {translated_text}"
                                ),
                                "notes": ["credential-free verification output"],
                            }
                        ],
                        "warnings": [],
                    },
                    ensure_ascii=False,
                ),
                finish_reason="stop",
            )
        if request.user_prompt.startswith("Summarize"):
            return StructuredCompletionResult(
                content=json.dumps(
                    {
                        "brief": " ".join(item["text"] for item in items),
                        "key_points": [
                            {
                                "text": "Verified summary point",
                                "evidence_item_ids": [
                                    item["item_id"] for item in items
                                ],
                            }
                        ],
                        "warnings": [],
                    },
                    ensure_ascii=False,
                ),
                finish_reason="stop",
            )
        if request.user_prompt.startswith("Outline"):
            return StructuredCompletionResult(
                content=json.dumps(
                    {
                        "chapters": [
                            {
                                "title": "Verified chapter",
                                "summary": " ".join(
                                    item["text"] for item in items
                                ),
                                "evidence_item_ids": [
                                    item["item_id"] for item in items
                                ],
                            }
                        ],
                        "warnings": [],
                    },
                    ensure_ascii=False,
                ),
                finish_reason="stop",
            )
        return StructuredCompletionResult(
            content=json.dumps(
                {
                    "title": "Stage 2E local verification",
                    "sections": [
                        {
                            "source_item_ids": [item["item_id"] for item in items],
                            "clean_text": " ".join(item["text"] for item in items),
                            "notes": [],
                        }
                    ],
                    "warnings": [],
                },
                ensure_ascii=False,
            ),
            finish_reason="stop",
        )


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


def _select_package(db_session, session_id: str | None):
    sessions = SessionRepository(db_session)
    candidates = (
        [sessions.get_required(session_id)]
        if session_id is not None
        else sessions.list_newest_first()
    )
    packages = PackageRepository(db_session)
    for candidate in candidates:
        for record in packages.list_for_session(candidate.id):
            try:
                package = PackageReader(packages).read(record.id)
                if PackageReader.effective_source(package).content.items:
                    return package
            except (LookupError, ValueError):
                continue
        try:
            package = PackageBuilder(db_session).build_baseline(candidate.id)
        except PackageBuildError:
            db_session.rollback()
            continue
        if PackageReader.effective_source(package).content.items:
            return package
        db_session.rollback()
    raise RuntimeError("No Frozen Package with source items could be found or built")


async def _run_jobs(
    database: Database,
    registry: ProcessorRegistry,
    package_id: str,
    *,
    target_language: str,
    timeout_seconds: float,
) -> dict[str, object]:
    runner = ProcessingJobRunner(database, registry, concurrency=1)
    await runner.start()
    try:
        completed: dict[str, object] = {}
        requests = (
            ("clean_script", {"style": "verification"}),
            (
                "refined_translation",
                {
                    "target_language": target_language,
                    "glossary": {"最终文本": "final transcript"},
                    "style": "verification",
                    "context_window_items": 1,
                },
            ),
            ("summary", {"style": "verification"}),
            ("chapter_outline", {"style": "verification"}),
        )
        for artifact_kind, options in requests:
            submitted = await runner.submit(
                package_id=package_id,
                artifact_kind=artifact_kind,
                options=options,
            )
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            while asyncio.get_running_loop().time() < deadline:
                with database.session() as db_session:
                    current = ProcessingJobRepository(db_session).get(
                        submitted.job_id
                    )
                if current is None:
                    raise RuntimeError("Processing Job disappeared")
                if current.status == "completed":
                    completed[artifact_kind] = current
                    break
                if current.status in {"failed", "cancelled"}:
                    raise RuntimeError(
                        f"{artifact_kind} Job ended as {current.status}: "
                        f"{current.error_code or 'no_error_code'}"
                    )
                await asyncio.sleep(0.01)
            else:
                await runner.cancel(submitted.job_id)
                raise TimeoutError(
                    f"{artifact_kind} Job did not finish before the verifier timeout"
                )
        summary_job = completed["summary"]
        summary_artifact_id = summary_job.result_artifact_id
        if summary_artifact_id is None:
            raise RuntimeError("Completed summary Job has no result Artifact")
        submitted = await runner.submit(
            package_id=package_id,
            artifact_kind="timeline_fact_review",
            options={},
            target_artifact_id=summary_artifact_id,
        )
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            with database.session() as db_session:
                current = ProcessingJobRepository(db_session).get(submitted.job_id)
            if current is None:
                raise RuntimeError("timeline_fact_review Job disappeared")
            if current.status == "completed":
                completed["timeline_fact_review"] = current
                break
            if current.status in {"failed", "cancelled"}:
                raise RuntimeError(
                    f"timeline_fact_review Job ended as {current.status}: "
                    f"{current.error_code or 'no_error_code'}"
                )
            await asyncio.sleep(0.01)
        else:
            await runner.cancel(submitted.job_id)
            raise TimeoutError(
                "timeline_fact_review Job did not finish before the verifier timeout"
            )
        return completed
    finally:
        await runner.stop()


def _verify_exports(artifact: DerivedArtifact) -> dict[str, int]:
    exporter = ArtifactExporter()
    formats = (
        ("json", "markdown")
        if artifact.artifact_kind == "clean_script"
        else ("json", "markdown", "srt", "vtt")
    )
    sizes: dict[str, int] = {}
    for export_format in formats:
        first = exporter.export(artifact, export_format)
        second = exporter.export(artifact, export_format)
        if first != second:
            raise RuntimeError(
                f"{artifact.artifact_kind} {export_format} export is not deterministic"
            )
        sizes[export_format] = len(first.content)

    payload = json.loads(exporter.export(artifact, "json").content)
    for section, evidence in zip(
        payload["content"]["sections"],
        artifact.evidence,
        strict=True,
    ):
        if section["start_ms"] != evidence.start_ms:
            raise RuntimeError("Export start time is not evidence-derived")
        if section["end_ms"] != evidence.end_ms:
            raise RuntimeError("Export end time is not evidence-derived")
        if section["source_segment_ids"] != list(evidence.source_segment_ids):
            raise RuntimeError("Export Segment IDs are not evidence-derived")

    if artifact.artifact_kind == "refined_translation":
        srt = exporter.export(artifact, "srt").content.decode("utf-8")
        vtt = exporter.export(artifact, "vtt").content.decode("utf-8")
        for evidence in artifact.evidence:
            srt_timing = (
                f"{format_timestamp(evidence.start_ms, separator=',')} --> "
                f"{format_timestamp(evidence.end_ms, separator=',')}"
            )
            vtt_timing = (
                f"{format_timestamp(evidence.start_ms)} --> "
                f"{format_timestamp(evidence.end_ms)}"
            )
            if srt_timing not in srt or vtt_timing not in vtt:
                raise RuntimeError("Subtitle timing is not evidence-derived")
    return sizes


def verify(
    *,
    session_id: str | None,
    database_url: str | None,
    target_language: str,
    timeout_seconds: float,
) -> dict[str, object]:
    resolved_database_url = database_url or get_settings().database_url
    database = Database(resolved_database_url)
    provider = LocalStructuredProvider()
    registry = ProcessorRegistry()
    registry.register(
        CleanScriptWorkflow(
            provider,
            max_retries=0,
            max_input_chars=10_000,
            max_items_per_chunk=50,
            retry_delay_seconds=0,
        )
    )
    registry.register(
        RefinedTranslationWorkflow(
            provider,
            max_retries=0,
            max_input_chars=10_000,
            max_items_per_chunk=50,
            retry_delay_seconds=0,
        )
    )
    registry.register(
        SummaryWorkflow(
            provider,
            max_retries=0,
            max_input_chars=10_000,
            max_items_per_chunk=50,
            retry_delay_seconds=0,
        )
    )
    registry.register(
        ChapterOutlineWorkflow(
            provider,
            max_retries=0,
            max_input_chars=10_000,
            max_items_per_chunk=50,
            retry_delay_seconds=0,
        )
    )
    registry.register(
        TimelineFactReviewWorkflow(
            provider,
            max_retries=0,
            max_input_chars=10_000,
            max_items_per_chunk=50,
            retry_delay_seconds=0,
        )
    )
    try:
        with database.session() as db_session:
            package = _select_package(db_session, session_id)
            selected_session_id = package.session_id
            package_hash = package.content_hash
            package_version = package.package_version
            source_item_count = len(
                PackageReader.effective_source(package).content.items
            )
            stage1_before = _stage1_snapshot(db_session, selected_session_id)
            db_session.commit()

        jobs = asyncio.run(
            _run_jobs(
                database,
                registry,
                package.package_id,
                target_language=target_language,
                timeout_seconds=timeout_seconds,
            )
        )
        for artifact_kind, job in jobs.items():
            if job.result_artifact_id is None:
                raise RuntimeError(
                    f"Completed {artifact_kind} Job has no result Artifact"
                )

        with database.session() as db_session:
            reloaded_package = PackageReader(
                PackageRepository(db_session)
            ).read(package.package_id)
            artifact_repository = ArtifactRepository(db_session)
            persisted = {
                artifact_kind: artifact_repository.get(job.result_artifact_id)
                for artifact_kind, job in jobs.items()
            }
            if any(artifact is None for artifact in persisted.values()):
                raise RuntimeError("Completed Processing Job Artifact was not persisted")
            clean_artifact = persisted["clean_script"]
            refined_artifact = persisted["refined_translation"]
            summary_artifact = persisted["summary"]
            chapter_artifact = persisted["chapter_outline"]
            review_artifact = persisted["timeline_fact_review"]
            assert clean_artifact is not None
            assert refined_artifact is not None
            assert summary_artifact is not None
            assert chapter_artifact is not None
            assert review_artifact is not None
            validator = EvidenceValidator()
            for artifact in persisted.values():
                assert artifact is not None
                validator.validate(
                    reloaded_package,
                    artifact.evidence,
                    require_complete_source=artifact.artifact_kind in {
                        "clean_script",
                        "refined_translation",
                    },
                )
                if artifact.package_content_hash != package_hash:
                    raise RuntimeError(
                        f"{artifact.artifact_kind} does not pin its input Package hash"
                    )
            if reloaded_package.content_hash != package_hash:
                raise RuntimeError("Input Package content hash changed")
            if refined_artifact.target_language != target_language:
                raise RuntimeError("Refined translation target language changed")
            summary_point = summary_artifact.content["key_points"][0]
            if summary_point["start_ms"] != summary_artifact.evidence[0].start_ms:
                raise RuntimeError("Summary time is not evidence-derived")
            if not summary_point["evidence_excerpts"]:
                raise RuntimeError("Summary has no server-derived evidence excerpt")
            chapter = chapter_artifact.content["chapters"][0]
            if chapter["start_ms"] != chapter_artifact.evidence[0].start_ms:
                raise RuntimeError("Chapter time is not evidence-derived")
            if review_artifact.parent_artifact_id != summary_artifact.artifact_id:
                raise RuntimeError("Fact review is not bound to the summary Artifact")
            if review_artifact.options != {
                "target_artifact_id": summary_artifact.artifact_id,
                "target_artifact_version": summary_artifact.artifact_version,
            }:
                raise RuntimeError("Fact review target identity was not persisted")
            review = review_artifact.content["reviews"][0]
            if review["status"] != "supported" or not review["evidence_excerpts"]:
                raise RuntimeError("Fact review did not preserve supported evidence")
            stage1_after = _stage1_snapshot(db_session, selected_session_id)
            if stage1_after != stage1_before:
                raise RuntimeError("Stage 1 Final evidence changed")

        clean_exports = _verify_exports(clean_artifact)
        refined_exports = _verify_exports(refined_artifact)

        return {
            "status": "ok",
            "session_id": selected_session_id,
            "package_id": package.package_id,
            "package_version": package_version,
            "package_content_hash": package_hash,
            "package_hash_unchanged": True,
            "source_item_count": source_item_count,
            "clean_script": {
                "job_id": jobs["clean_script"].job_id,
                "job_status": jobs["clean_script"].status,
                "job_progress": jobs["clean_script"].progress,
                "artifact_id": clean_artifact.artifact_id,
                "artifact_version": clean_artifact.artifact_version,
                "evidence_count": len(clean_artifact.evidence),
                "export_bytes": clean_exports,
            },
            "refined_translation": {
                "job_id": jobs["refined_translation"].job_id,
                "job_status": jobs["refined_translation"].status,
                "job_progress": jobs["refined_translation"].progress,
                "artifact_id": refined_artifact.artifact_id,
                "artifact_version": refined_artifact.artifact_version,
                "target_language": refined_artifact.target_language,
                "evidence_count": len(refined_artifact.evidence),
                "export_bytes": refined_exports,
            },
            "summary": {
                "job_id": jobs["summary"].job_id,
                "job_status": jobs["summary"].status,
                "job_progress": jobs["summary"].progress,
                "artifact_id": summary_artifact.artifact_id,
                "artifact_version": summary_artifact.artifact_version,
                "evidence_count": len(summary_artifact.evidence),
                "key_point_count": len(summary_artifact.content["key_points"]),
            },
            "chapter_outline": {
                "job_id": jobs["chapter_outline"].job_id,
                "job_status": jobs["chapter_outline"].status,
                "job_progress": jobs["chapter_outline"].progress,
                "artifact_id": chapter_artifact.artifact_id,
                "artifact_version": chapter_artifact.artifact_version,
                "evidence_count": len(chapter_artifact.evidence),
                "chapter_count": len(chapter_artifact.content["chapters"]),
            },
            "timeline_fact_review": {
                "job_id": jobs["timeline_fact_review"].job_id,
                "job_status": jobs["timeline_fact_review"].status,
                "job_progress": jobs["timeline_fact_review"].progress,
                "artifact_id": review_artifact.artifact_id,
                "artifact_version": review_artifact.artifact_version,
                "target_artifact_id": review_artifact.parent_artifact_id,
                "evidence_count": len(review_artifact.evidence),
                "review_count": len(review_artifact.content["reviews"]),
            },
            "evidence_valid": True,
            "provider": clean_artifact.provider,
            "provider_request_count": provider.request_count,
            "live_reference_item_count": provider.live_reference_item_count,
            "stage1_final_count": len(stage1_after),
            "stage1_final_unchanged": True,
        }
    finally:
        database.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run a credential-free Frozen Package -> Processing Job -> "
            "Stage 2C through 2E Artifact verification."
        ),
    )
    parser.add_argument("--session-id")
    parser.add_argument(
        "--database-url",
        help="Override DATABASE_URL; intended for isolated local verification.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--target-language", default="en-US")
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    try:
        result = verify(
            session_id=args.session_id,
            database_url=args.database_url,
            target_language=args.target_language,
            timeout_seconds=args.timeout_seconds,
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
