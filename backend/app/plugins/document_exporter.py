from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from app.persistence.models import PluginDocumentRecord


PluginDocumentExportFormat = Literal["markdown", "json"]


@dataclass(frozen=True, slots=True)
class PluginDocumentExport:
    content: bytes
    media_type: str
    filename: str


class PluginDocumentExporter:
    def export(
        self,
        record: PluginDocumentRecord,
        export_format: PluginDocumentExportFormat,
    ) -> PluginDocumentExport:
        stem = self._safe_stem(record)
        if export_format == "markdown":
            return PluginDocumentExport(
                content=record.markdown_text.encode("utf-8"),
                media_type="text/markdown; charset=utf-8",
                filename=f"{stem}.md",
            )
        if export_format != "json":
            raise ValueError("unsupported plugin document export format")
        payload = {
            "metadata": {
                "document_id": record.id,
                "plugin_id": record.plugin_id,
                "plugin_version": record.plugin_version,
                "media_session_id": record.media_session_id,
                "source_package_id": record.source_package_id,
                "source_package_version": record.source_package_version,
                "source_package_hash": record.source_package_hash,
                "identity_key": record.identity_key,
                "document_version": record.document_version,
                "schema_name": record.schema_name,
                "schema_version": record.schema_version,
                "language": record.language,
                "trigger": record.trigger,
                "completeness": record.completeness,
                "status": record.status,
                "content_hash": record.content_hash,
                "created_at": record.created_at.isoformat(),
            },
            "content": record.content_json,
            "evidence_refs": record.evidence_refs_json,
            "markdown": record.markdown_text,
        }
        return PluginDocumentExport(
            content=(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8"),
            media_type="application/json; charset=utf-8",
            filename=f"{stem}.json",
        )

    @staticmethod
    def _safe_stem(record: PluginDocumentRecord) -> str:
        normalized = re.sub(r"[^A-Za-z0-9.-]+", "-", record.identity_key)
        normalized = normalized.strip(".-") or "plugin-document"
        normalized = normalized[:120].rstrip(".-") or "plugin-document"
        return f"{normalized}-v{record.document_version}"


__all__ = [
    "PluginDocumentExport",
    "PluginDocumentExporter",
    "PluginDocumentExportFormat",
]
