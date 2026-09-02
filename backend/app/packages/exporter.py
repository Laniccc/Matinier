from __future__ import annotations

import io
import json
import re
import zipfile

from app.export import (
    ExportDocument,
    export_markdown,
    export_srt,
    export_vtt,
)
from app.packages.canonical import canonical_json, sha256_hex
from app.packages.models import (
    EvidenceIndexDocument,
    LiveTranslationDocument,
    MetricsMetadataDocument,
    PackageDocument,
    ProviderMetadataDocument,
    SessionMetadataDocument,
    SourceApprovedDocument,
    SourceRawDocument,
    TimelineIndexDocument,
    TranscriptPackage,
)
from app.packages.validator import PackageValidator
from app.persistence.models import SessionRecord
from app.timeline.models import Timeline, TimelineSegment


_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _json_file(value: object) -> bytes:
    return canonical_json(value) + b"\n"


def _safe_language(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "_", value)


def _timeline(
    package: TranscriptPackage,
    document: SourceRawDocument | SourceApprovedDocument | LiveTranslationDocument,
) -> Timeline:
    created_at = package.created_at
    segments = tuple(
        TimelineSegment(
            id=item.item_id,
            session_id=package.session_id,
            segment_id=item.item_id,
            track_id=document.document_kind,
            revision=1,
            language=document.language,
            raw_text=item.raw_text or item.text,
            display_text=item.text,
            audio_start_ms=item.start_ms,
            audio_end_ms=item.end_ms,
            confidence=item.confidence,
            received_at_ms=int(created_at.timestamp() * 1_000),
            finalized_at=created_at,
            created_at=created_at,
            updated_at=created_at,
        )
        for item in document.content.items
    )
    return Timeline(
        segments=segments,
        duration_ms=max((item.audio_end_ms for item in segments), default=0),
    )


def _export_session(package: TranscriptPackage) -> SessionRecord:
    snapshot = package.manifest.session_snapshot
    provider = package.manifest.provider_snapshot
    metrics = package.manifest.metrics_snapshot
    return SessionRecord(
        id=package.session_id,
        room_name=snapshot.room_name,
        status="completed",
        source_type=snapshot.source_type,
        source_name="Frozen canonical transcript package",
        language=snapshot.source_language,
        target_language=(
            snapshot.target_languages[0] if snapshot.target_languages else None
        ),
        asr_provider=provider.asr_provider,
        asr_model=provider.asr_model,
        final_result_count=metrics.final_result_count,
        first_partial_latency_ms=metrics.first_partial_latency_ms,
        average_final_latency_ms=metrics.average_final_latency_ms,
        provider_error_count=metrics.provider_error_count,
        sent_audio_chunk_count=metrics.sent_audio_chunk_count,
        sent_audio_bytes=metrics.sent_audio_bytes,
        started_at=snapshot.started_at,
        ended_at=snapshot.ended_at,
        created_at=snapshot.created_at,
    )


def _document_path(document: PackageDocument) -> str:
    if isinstance(document, SourceRawDocument):
        return "documents/source.raw.json"
    if isinstance(document, SourceApprovedDocument):
        return "documents/source.approved.json"
    if isinstance(document, LiveTranslationDocument):
        return (
            "documents/translation."
            f"{_safe_language(document.language)}.live.json"
        )
    if isinstance(document, TimelineIndexDocument):
        return "indexes/timeline.json"
    if isinstance(document, EvidenceIndexDocument):
        return "indexes/evidence.json"
    if isinstance(document, SessionMetadataDocument):
        return "metadata/session.json"
    if isinstance(document, ProviderMetadataDocument):
        return "metadata/providers.json"
    if isinstance(document, MetricsMetadataDocument):
        return "metadata/metrics.json"
    raise TypeError(f"Unsupported package document: {document.document_kind}")


def verify_package_zip(value: bytes) -> tuple[bool, tuple[str, ...]]:
    errors: list[str] = []
    with zipfile.ZipFile(io.BytesIO(value)) as archive:
        names = archive.namelist()
        checksum_names = [name for name in names if name.endswith("checksums.json")]
        if len(checksum_names) != 1:
            return False, ("ZIP must contain exactly one checksums.json",)
        root = checksum_names[0].removesuffix("checksums.json")
        payload = json.loads(archive.read(checksum_names[0]))
        files = payload.get("files")
        if payload.get("algorithm") != "sha256" or not isinstance(files, dict):
            return False, ("checksums.json has an invalid schema",)
        expected_files = {
            name.removeprefix(root)
            for name in names
            if name != checksum_names[0]
        }
        if set(files) != expected_files:
            errors.append("checksums file index does not match ZIP entries")
        for path, expected in files.items():
            full_path = root + path
            if full_path not in names:
                continue
            if sha256_hex(archive.read(full_path)) != expected:
                errors.append(f"checksum mismatch: {path}")
    return not errors, tuple(errors)


class PackageZipExporter:
    def __init__(self, validator: PackageValidator | None = None) -> None:
        self._validator = validator or PackageValidator()

    def export(self, package: TranscriptPackage) -> bytes:
        self._validator.validate_or_raise(package)
        session = _export_session(package)
        files: dict[str, bytes] = {
            "manifest.json": _json_file(package.manifest),
        }
        source_documents: dict[
            str, SourceRawDocument | SourceApprovedDocument
        ] = {}
        translations: list[LiveTranslationDocument] = []
        for document in package.documents:
            files[_document_path(document)] = _json_file(document)
            if isinstance(document, (SourceRawDocument, SourceApprovedDocument)):
                source_documents[document.document_id] = document
            elif isinstance(document, LiveTranslationDocument):
                translations.append(document)
        source_document = source_documents.get(
            package.manifest.effective_source_document_id
        )
        if source_document is None:
            raise ValueError("Package has no effective source document")

        source_export = ExportDocument(
            session=session,
            timeline=_timeline(package, source_document),
            exported_at=package.created_at,
        )
        files["rendered/source.srt"] = export_srt(source_export)
        files["rendered/source.vtt"] = export_vtt(source_export)
        files["rendered/source.md"] = export_markdown(source_export)
        for document in sorted(translations, key=lambda item: item.language):
            translation_export = ExportDocument(
                session=session,
                timeline=_timeline(package, document),
                content="translation",
                exported_at=package.created_at,
            )
            files[
                "rendered/translation."
                f"{_safe_language(document.language)}.srt"
            ] = export_srt(translation_export)

        checksums = {
            path: sha256_hex(content)
            for path, content in sorted(files.items())
        }
        files["checksums.json"] = _json_file(
            {"algorithm": "sha256", "files": checksums}
        )
        root = f"package-v{package.package_version}/"
        output = io.BytesIO()
        with zipfile.ZipFile(
            output,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for path, content in sorted(files.items()):
                info = zipfile.ZipInfo(root + path, date_time=_ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, content, compresslevel=9)
        return output.getvalue()
