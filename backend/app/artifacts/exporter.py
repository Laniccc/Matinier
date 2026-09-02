from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Literal

from app.artifacts.models import ArtifactEvidence, DerivedArtifact
from app.export.models import format_timestamp, normalize_caption_text, one_line
from app.text_processing.markdown import render_processed_script_markdown
from app.text_processing.models import ProcessedScriptContent, ScriptSection


ArtifactExportFormat = Literal["json", "srt", "vtt", "markdown"]


class ArtifactExportError(ValueError):
    """An Artifact cannot be rendered under its declared schema."""


@dataclass(frozen=True, slots=True)
class ArtifactExportResult:
    content: bytes
    media_type: str
    filename: str


@dataclass(frozen=True, slots=True)
class _ExportSection:
    evidence: ArtifactEvidence
    text: str
    source_text: str | None
    notes: tuple[str, ...]
    normalized_content: dict[str, object]


def _text_list(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise ArtifactExportError(f"Artifact {field} must be a string array")
    return tuple(value)


def _normalized_sections(artifact: DerivedArtifact) -> tuple[_ExportSection, ...]:
    raw_sections = artifact.content.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise ArtifactExportError("Artifact has no exportable sections")
    if len(raw_sections) != len(artifact.evidence):
        raise ArtifactExportError("Artifact sections and evidence do not align")

    sections: list[_ExportSection] = []
    previous_start = -1
    for index, (raw, evidence) in enumerate(
        zip(raw_sections, artifact.evidence, strict=True)
    ):
        if not isinstance(raw, dict):
            raise ArtifactExportError("Artifact section must be an object")
        if evidence.evidence_key != f"section:{index}":
            raise ArtifactExportError("Artifact evidence order is invalid")
        source_item_ids = _text_list(
            raw.get("source_item_ids"),
            field="source_item_ids",
        )
        if source_item_ids != evidence.source_item_ids:
            raise ArtifactExportError(
                "Artifact section source items do not match evidence"
            )
        if evidence.start_ms < previous_start:
            raise ArtifactExportError("Artifact evidence is not time ordered")
        previous_start = evidence.start_ms

        text_field = (
            "clean_text"
            if artifact.artifact_kind == "clean_script"
            else "translated_text"
        )
        text = raw.get(text_field)
        if not isinstance(text, str) or not text.strip():
            raise ArtifactExportError(f"Artifact {text_field} must not be blank")
        source_text = raw.get("source_text")
        if source_text is not None and not isinstance(source_text, str):
            raise ArtifactExportError("Artifact source_text must be text")
        notes = _text_list(raw.get("notes", []), field="notes")
        normalized_content = dict(raw)
        normalized_content.update(
            {
                "source_item_ids": list(evidence.source_item_ids),
                "source_segment_ids": list(evidence.source_segment_ids),
                "start_ms": evidence.start_ms,
                "end_ms": evidence.end_ms,
                text_field: text.strip(),
                "notes": list(notes),
            }
        )
        sections.append(
            _ExportSection(
                evidence=evidence,
                text=text.strip(),
                source_text=source_text,
                notes=notes,
                normalized_content=normalized_content,
            )
        )
    return tuple(sections)


def _warnings(artifact: DerivedArtifact) -> tuple[str, ...]:
    return _text_list(artifact.content.get("warnings", []), field="warnings")


def _filename_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9.-]+", "-", value).strip("-.")
    return token or "unknown"


def _json_export(
    artifact: DerivedArtifact,
    sections: tuple[_ExportSection, ...] | None = None,
) -> bytes:
    payload = artifact.model_dump(mode="json")
    if sections is not None:
        content = dict(payload["content"])
        content["sections"] = [
            section.normalized_content for section in sections
        ]
        payload["content"] = content
    try:
        rendered = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ArtifactExportError("Artifact contains non-JSON values") from error
    return f"{rendered}\n".encode("utf-8")


def _clean_markdown(
    artifact: DerivedArtifact,
    sections: tuple[_ExportSection, ...],
) -> bytes:
    legacy = artifact.content.get("legacy_markdown_text")
    if isinstance(legacy, str):
        return legacy.encode("utf-8")
    title = artifact.content.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ArtifactExportError("Clean script title must not be blank")
    content = ProcessedScriptContent(
        title=title.strip(),
        sections=tuple(
            ScriptSection(
                source_segment_ids=section.evidence.source_segment_ids,
                start_ms=section.evidence.start_ms,
                end_ms=section.evidence.end_ms,
                clean_text=section.text,
                notes=section.notes,
            )
            for section in sections
        ),
        warnings=_warnings(artifact),
    )
    rendered = render_processed_script_markdown(
        content,
        provider=artifact.provider or "unknown",
        model=artifact.model or "unknown",
        version=artifact.artifact_version,
        created_at=artifact.created_at,
    )
    return rendered.encode("utf-8")


def _translation_markdown(
    artifact: DerivedArtifact,
    sections: tuple[_ExportSection, ...],
) -> bytes:
    target_language = artifact.target_language
    if target_language is None:
        raise ArtifactExportError("Refined translation has no target language")
    lines = [
        f"# Refined translation — {one_line(target_language)}",
        "",
        f"Package: `{one_line(artifact.package_id)}` v{artifact.package_version}",
        f"Package hash: `{artifact.package_content_hash}`",
        f"Artifact version: {artifact.artifact_version}",
        f"Provider: {one_line(artifact.provider or 'unknown')}",
        f"Model: {one_line(artifact.model or 'unknown')}",
        f"Workflow: {one_line(artifact.workflow_version)}",
        "",
    ]
    for section in sections:
        start = format_timestamp(section.evidence.start_ms)
        end = format_timestamp(section.evidence.end_ms)
        lines.extend([f"[{start} - {end}]", section.text, ""])
        if section.source_text:
            lines.extend([f"> Source: {section.source_text}", ""])
        lines.append(
            "Source items: "
            + ", ".join(
                f"`{one_line(item_id)}`"
                for item_id in section.evidence.source_item_ids
            )
        )
        if section.notes:
            lines.append("Notes: " + "; ".join(section.notes))
        lines.append("")
    warnings = _warnings(artifact)
    if warnings:
        lines.extend(
            ["## Warnings", "", *(f"- {warning}" for warning in warnings), ""]
        )
    return "\n".join(lines).encode("utf-8")


def _srt_export(sections: tuple[_ExportSection, ...]) -> bytes:
    cues = [
        (
            f"{index}\n"
            f"{format_timestamp(section.evidence.start_ms, separator=',')} --> "
            f"{format_timestamp(section.evidence.end_ms, separator=',')}\n"
            f"{normalize_caption_text(section.text)}"
        )
        for index, section in enumerate(sections, start=1)
    ]
    return ("\n\n".join(cues) + "\n").encode("utf-8")


def _vtt_export(sections: tuple[_ExportSection, ...]) -> bytes:
    cues = [
        (
            f"{format_timestamp(section.evidence.start_ms)} --> "
            f"{format_timestamp(section.evidence.end_ms)}\n"
            f"{html.escape(normalize_caption_text(section.text), quote=False)}"
        )
        for section in sections
    ]
    return ("WEBVTT\n\n" + "\n\n".join(cues) + "\n").encode("utf-8")


class ArtifactExporter:
    def export(
        self,
        artifact: DerivedArtifact,
        export_format: ArtifactExportFormat,
    ) -> ArtifactExportResult:
        section_kind = artifact.artifact_kind in {
            "clean_script",
            "refined_translation",
        }
        if not section_kind and export_format != "json":
            raise ArtifactExportError(
                f"{artifact.artifact_kind} supports only json export"
            )
        if artifact.artifact_kind == "clean_script" and export_format not in {
            "json",
            "markdown",
        }:
            raise ArtifactExportError(
                "clean_script supports only json and markdown export"
            )
        if artifact.artifact_kind == "refined_translation":
            content_language = artifact.content.get("target_language")
            if (
                artifact.target_language is None
                or content_language != artifact.target_language
            ):
                raise ArtifactExportError(
                    "Refined translation target language identity is inconsistent"
                )
        identity = artifact.artifact_kind.replace("_", "-")
        if artifact.target_language:
            identity += f"-{_filename_token(artifact.target_language)}"
        filename_base = f"{identity}-v{artifact.artifact_version}"
        if export_format == "json":
            sections = _normalized_sections(artifact) if section_kind else None
            return ArtifactExportResult(
                content=_json_export(artifact, sections),
                media_type="application/json",
                filename=f"{filename_base}.json",
            )
        sections = _normalized_sections(artifact)
        if export_format == "markdown":
            renderer = (
                _clean_markdown
                if artifact.artifact_kind == "clean_script"
                else _translation_markdown
            )
            return ArtifactExportResult(
                content=renderer(artifact, sections),
                media_type="text/markdown",
                filename=f"{filename_base}.md",
            )
        if export_format == "srt":
            return ArtifactExportResult(
                content=_srt_export(sections),
                media_type="application/x-subrip",
                filename=f"{filename_base}.srt",
            )
        if export_format == "vtt":
            return ArtifactExportResult(
                content=_vtt_export(sections),
                media_type="text/vtt",
                filename=f"{filename_base}.vtt",
            )
        raise ArtifactExportError(f"Unsupported Artifact export: {export_format}")
