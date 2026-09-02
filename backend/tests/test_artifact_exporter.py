from __future__ import annotations

import datetime as dt
import json

import pytest

from app.artifacts.exporter import ArtifactExporter, ArtifactExportError
from app.artifacts.models import ArtifactEvidence, DerivedArtifact


NOW = dt.datetime(2026, 8, 10, 9, tzinfo=dt.UTC)


def _artifact(kind: str) -> DerivedArtifact:
    refined = kind == "refined_translation"
    content = {
        "sections": [
            {
                "source_item_ids": ["item-1"],
                "source_segment_ids": ["untrusted-segment"],
                "start_ms": 90_000,
                "end_ms": 99_000,
                "notes": ["checked"],
                **(
                    {
                        "source_text": "你好 <world>",
                        "translated_text": "Hello <world>",
                    }
                    if refined
                    else {"clean_text": "你好，世界。"}
                ),
            }
        ],
        "warnings": ["review name"],
        **({"target_language": "en-US"} if refined else {"title": "整理版"}),
    }
    return DerivedArtifact(
        artifact_id=f"artifact-{kind}",
        package_id="package-1",
        package_version=2,
        package_content_hash="a" * 64,
        artifact_kind=kind,
        identity_key=(
            "refined_translation:en-US" if refined else "clean_script"
        ),
        artifact_version=3,
        target_language="en-US" if refined else None,
        status="generated",
        provider="fake",
        model="fake-v1",
        workflow_version="1.0",
        options={"target_language": "en-US"} if refined else {},
        content=content,
        evidence=(
            ArtifactEvidence(
                evidence_key="section:0",
                source_item_ids=("item-1",),
                source_segment_ids=("segment-1",),
                start_ms=1_234,
                end_ms=5_678,
            ),
        ),
        parent_artifact_id=None,
        created_by="model",
        created_at=NOW,
        approved_at=None,
    )


def test_refined_translation_exports_are_deterministic_and_use_evidence_time() -> None:
    artifact = _artifact("refined_translation")
    exporter = ArtifactExporter()

    json_export = exporter.export(artifact, "json")
    markdown = exporter.export(artifact, "markdown")
    srt = exporter.export(artifact, "srt")
    vtt = exporter.export(artifact, "vtt")

    payload = json.loads(json_export.content)
    section = payload["content"]["sections"][0]
    assert section["start_ms"] == 1_234
    assert section["end_ms"] == 5_678
    assert section["source_segment_ids"] == ["segment-1"]
    assert json_export.filename == "refined-translation-en-US-v3.json"
    assert srt.content.decode("utf-8") == (
        "1\n00:00:01,234 --> 00:00:05,678\nHello <world>\n"
    )
    assert vtt.content.decode("utf-8") == (
        "WEBVTT\n\n00:00:01.234 --> 00:00:05.678\n"
        "Hello &lt;world&gt;\n"
    )
    markdown_text = markdown.content.decode("utf-8")
    assert "# Refined translation — en-US" in markdown_text
    assert "[00:00:01.234 - 00:00:05.678]" in markdown_text
    assert "> Source: 你好 <world>" in markdown_text
    for export_format in ("json", "markdown", "srt", "vtt"):
        assert exporter.export(artifact, export_format) == exporter.export(
            artifact,
            export_format,
        )


def test_clean_script_supports_json_and_markdown_only() -> None:
    artifact = _artifact("clean_script")
    exporter = ArtifactExporter()

    json_export = exporter.export(artifact, "json")
    markdown = exporter.export(artifact, "markdown")

    assert json.loads(json_export.content)["content"]["sections"][0][
        "start_ms"
    ] == 1_234
    assert markdown.filename == "clean-script-v3.md"
    assert "# 整理版" in markdown.content.decode("utf-8")
    assert "[00:00:01.234 - 00:00:05.678]" in markdown.content.decode("utf-8")
    with pytest.raises(ArtifactExportError, match="only json and markdown"):
        exporter.export(artifact, "srt")


def test_factual_artifact_supports_standalone_json_export() -> None:
    artifact = _artifact("clean_script").model_copy(
        update={
            "artifact_kind": "summary",
            "identity_key": "summary",
            "content": {
                "brief": "Verified summary",
                "key_points": [],
                "warnings": [],
            },
            "evidence": (),
        }
    )

    exported = ArtifactExporter().export(artifact, "json")

    assert exported.filename == "summary-v3.json"
    assert json.loads(exported.content)["content"]["brief"] == (
        "Verified summary"
    )
    with pytest.raises(ArtifactExportError, match="supports only json"):
        ArtifactExporter().export(artifact, "markdown")


def test_export_rejects_content_that_does_not_match_server_evidence() -> None:
    artifact = _artifact("refined_translation")
    artifact.content["sections"][0]["source_item_ids"] = ["foreign-item"]

    with pytest.raises(ArtifactExportError, match="do not match evidence"):
        ArtifactExporter().export(artifact, "json")


def test_export_rejects_refined_translation_language_identity_mismatch() -> None:
    artifact = _artifact("refined_translation")
    artifact.content["target_language"] = "fr-FR"

    with pytest.raises(ArtifactExportError, match="identity is inconsistent"):
        ArtifactExporter().export(artifact, "json")
