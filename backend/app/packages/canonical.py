from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from app.packages.models import PackageDocument, document_identity


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    return value


def canonical_json(value: object) -> bytes:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_sha256(value: object) -> str:
    return sha256_hex(canonical_json(value))


def document_content_hash(document: PackageDocument) -> str:
    # Storage/document IDs are deliberately outside content identity.
    return canonical_sha256(
        {
            "document_kind": document.document_kind,
            "language": document.language,
            "content": document.content.model_dump(mode="json"),
        }
    )


def package_content_hash(
    *,
    schema_name: str,
    schema_version: str,
    package_version: int,
    session_id: str,
    source_revision_id: str | None,
    documents: Sequence[PackageDocument],
) -> str:
    index: list[Mapping[str, Any]] = [
        {
            "document_kind": document.document_kind,
            "language": document.language,
            "content_hash": document.content_hash,
        }
        for document in sorted(documents, key=document_identity)
    ]
    return canonical_sha256(
        {
            "schema_name": schema_name,
            "schema_version": schema_version,
            "package_version": package_version,
            "session_id": session_id,
            "source_revision_id": source_revision_id,
            "documents": index,
        }
    )
