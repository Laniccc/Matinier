"""Migrate append-only processed scripts into Package artifacts.

Revision ID: 20260808_0016
Revises: 20260808_0015
Create Date: 2026-08-08
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op


revision: str = "20260808_0016"
down_revision: str | None = "20260808_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "matinier.transcript-package"
_SCHEMA_VERSION = "1.0"
_WORKFLOW_VERSION = "legacy-script-v1"


def _json(value: object) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _stable_id(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "matinier:" + ":".join(parts)))


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value.isoformat()
        rendered = value.isoformat()
        return rendered[:-6] + "Z" if rendered.endswith("+00:00") else rendered
    return str(value)


def _document(
    package_id: str,
    kind: str,
    language: str | None,
    content: Mapping[str, Any],
) -> dict[str, Any]:
    identity = [package_id, kind]
    if language is not None:
        identity.append(language)
    return {
        "id": _stable_id(*identity),
        "kind": kind,
        "language": language,
        "content": dict(content),
        "content_hash": _canonical_hash(
            {
                "document_kind": kind,
                "language": language,
                "content": content,
            }
        ),
    }


def _segment_ids(items: Sequence[Mapping[str, Any]]) -> set[str]:
    return {
        str(segment_id)
        for item in items
        for segment_id in item.get("source_segment_ids", [])
    }


def _compatible(
    items: Sequence[Mapping[str, Any]],
    snapshot: Sequence[Mapping[str, Any]],
    content: Mapping[str, Any],
) -> bool:
    snapshot_ids = {str(item["segment_id"]) for item in snapshot}
    if not snapshot_ids or _segment_ids(items) != snapshot_ids:
        return False
    section_sets = [
        {str(value) for value in section.get("source_segment_ids", [])}
        for section in content.get("sections", [])
    ]
    if not section_sets or any(not item for item in section_sets):
        return False
    for item in items:
        item_segments = {
            str(value) for value in item.get("source_segment_ids", [])
        }
        if sum(item_segments <= section for section in section_sets) != 1:
            return False
    return True


def _match_score(
    items: Sequence[Mapping[str, Any]],
    snapshot: Sequence[Mapping[str, Any]],
) -> int:
    source_by_id = {str(item["segment_id"]): item for item in snapshot}
    score = 0
    for item in items:
        source_ids = [str(value) for value in item["source_segment_ids"]]
        if len(source_ids) != 1:
            continue
        source = source_by_id[source_ids[0]]
        if item.get("text") == source.get("display_text"):
            score += 2
        if (
            item.get("start_ms") == source.get("audio_start_ms")
            and item.get("end_ms") == source.get("audio_end_ms")
        ):
            score += 1
    return score


def _legacy_package_id(
    session_id: str,
    snapshot: Sequence[Mapping[str, Any]],
) -> str:
    return _stable_id(
        "legacy-script-package",
        session_id,
        _canonical_hash(snapshot),
    )


def _build_legacy_package(
    connection: sa.Connection,
    tables: Mapping[str, sa.Table],
    *,
    session: Mapping[str, Any],
    snapshot: Sequence[Mapping[str, Any]],
    created_at: object,
) -> tuple[Mapping[str, Any], list[dict[str, Any]]]:
    session_id = str(session["id"])
    package_id = _legacy_package_id(session_id, snapshot)
    existing = connection.execute(
        sa.select(tables["result_packages"]).where(
            tables["result_packages"].c.id == package_id
        )
    ).mappings().first()
    if existing is not None:
        manifest = _json(existing["manifest_json"])
        source_id = manifest["effective_source_document_id"]
        source = connection.execute(
            sa.select(tables["package_documents"]).where(
                tables["package_documents"].c.id == source_id
            )
        ).mappings().one()
        return existing, list(_json(source["content_json"])["items"])

    current_version = connection.scalar(
        sa.select(sa.func.max(tables["result_packages"].c.version)).where(
            tables["result_packages"].c.session_id == session_id
        )
    )
    version = int(current_version or 0) + 1
    language = str(snapshot[0].get("language") or session["language"])
    items = [
        {
            "item_id": _stable_id(session_id, "source", str(item["segment_id"])),
            "source_segment_ids": [str(item["segment_id"])],
            "start_ms": int(item["audio_start_ms"]),
            "end_ms": int(item["audio_end_ms"]),
            "text": str(item["display_text"]),
            "raw_text": str(item.get("raw_text") or item["display_text"]),
            "confidence": None,
            "speaker": None,
            "locked": True,
        }
        for item in snapshot
    ]
    source_id = _stable_id(package_id, "source_raw", language)
    duration_ms = max((int(item["end_ms"]) for item in items), default=0)
    session_snapshot = {
        "session_id": session_id,
        "room_name": str(session["room_name"]),
        "status": str(session["status"]),
        "source_type": str(session["source_type"]),
        "source_language": language,
        "target_languages": [],
        "created_at": _iso(session["created_at"]),
        "started_at": _iso(session.get("started_at")),
        "ended_at": _iso(session.get("ended_at")),
        "source_ended_at": _iso(session.get("source_ended_at")),
        "translation_ended_at": _iso(session.get("translation_ended_at")),
        "stop_reason": session.get("stop_reason"),
        "failure_code": session.get("failure_code") or session.get("error_code"),
    }
    provider_snapshot = {
        "asr_provider": session.get("asr_provider"),
        "asr_model": session.get("asr_model"),
        "translation_provider": session.get("translation_provider"),
        "translation_model": session.get("translation_model"),
        "translation_status": str(
            session.get("translation_status") or "not_requested"
        ),
    }
    metrics_snapshot = {
        "final_result_count": session.get("final_result_count"),
        "first_partial_latency_ms": session.get("first_partial_latency_ms"),
        "average_final_latency_ms": session.get("average_final_latency_ms"),
        "provider_error_count": session.get("provider_error_count"),
        "sent_audio_chunk_count": session.get("sent_audio_chunk_count"),
        "sent_audio_bytes": session.get("sent_audio_bytes"),
    }
    documents = [
        _document(package_id, "source_raw", language, {"items": items}),
        _document(
            package_id,
            "timeline_index",
            None,
            {
                "duration_ms": duration_ms,
                "items": [
                    {
                        "item_id": item["item_id"],
                        "start_ms": item["start_ms"],
                        "end_ms": item["end_ms"],
                        "source_document_id": source_id,
                    }
                    for item in items
                ],
            },
        ),
        _document(
            package_id,
            "evidence_index",
            None,
            {
                "items": [
                    {
                        "item_id": item["item_id"],
                        "source_segment_ids": item["source_segment_ids"],
                        "start_ms": item["start_ms"],
                        "end_ms": item["end_ms"],
                        "raw_text": item["raw_text"],
                        "effective_text": item["text"],
                        "source_document_id": source_id,
                    }
                    for item in items
                ]
            },
        ),
        _document(package_id, "session_metadata", None, session_snapshot),
        _document(package_id, "provider_metadata", None, provider_snapshot),
        _document(package_id, "metrics_metadata", None, metrics_snapshot),
    ]
    documents.sort(key=lambda item: (item["kind"], item["language"] or ""))
    content_hash = _canonical_hash(
        {
            "schema_name": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "package_version": version,
            "session_id": session_id,
            "source_revision_id": None,
            "documents": [
                {
                    "document_kind": item["kind"],
                    "language": item["language"],
                    "content_hash": item["content_hash"],
                }
                for item in documents
            ],
        }
    )
    created = _iso(created_at)
    manifest = {
        "schema": _SCHEMA,
        "schema_version": _SCHEMA_VERSION,
        "package_id": package_id,
        "package_version": version,
        "session_id": session_id,
        "status": "frozen",
        "source_language": language,
        "target_languages": [],
        "effective_source_document_id": source_id,
        "source_revision_id": None,
        "created_at": created,
        "content_hash": content_hash,
        "documents": [
            {
                "document_id": item["id"],
                "document_kind": item["kind"],
                "language": item["language"],
                "content_hash": item["content_hash"],
            }
            for item in documents
        ],
        "session_snapshot": session_snapshot,
        "provider_snapshot": provider_snapshot,
        "metrics_snapshot": metrics_snapshot,
    }
    has_existing_packages = current_version is not None
    connection.execute(
        sa.insert(tables["result_packages"]).values(
            id=package_id,
            session_id=session_id,
            version=version,
            schema_name=_SCHEMA,
            schema_version=_SCHEMA_VERSION,
            source_revision_id=None,
            status="superseded" if has_existing_packages else "frozen",
            content_hash=content_hash,
            manifest_json=manifest,
            created_at=created_at,
            frozen_at=created_at,
            superseded_at=created_at if has_existing_packages else None,
        )
    )
    connection.execute(
        sa.insert(tables["package_documents"]),
        [
            {
                "id": item["id"],
                "package_id": package_id,
                "document_kind": item["kind"],
                "language": item["language"],
                "content_json": item["content"],
                "content_hash": item["content_hash"],
                "created_at": created_at,
            }
            for item in documents
        ],
    )
    package = {
        "id": package_id,
        "version": version,
        "content_hash": content_hash,
        "manifest_json": manifest,
    }
    return package, items


def _find_or_create_package(
    connection: sa.Connection,
    tables: Mapping[str, sa.Table],
    *,
    session: Mapping[str, Any],
    snapshot: Sequence[Mapping[str, Any]],
    content: Mapping[str, Any],
    created_at: object,
) -> tuple[Mapping[str, Any], list[dict[str, Any]]]:
    candidates: list[tuple[int, int, Mapping[str, Any], list[dict[str, Any]]]] = []
    package_rows = connection.execute(
        sa.select(tables["result_packages"])
        .where(
            tables["result_packages"].c.session_id == session["id"],
            tables["result_packages"].c.status.in_(("frozen", "superseded")),
        )
        .order_by(tables["result_packages"].c.version)
    ).mappings()
    for package in package_rows:
        manifest = _json(package["manifest_json"])
        source_id = manifest.get("effective_source_document_id")
        source = connection.execute(
            sa.select(tables["package_documents"]).where(
                tables["package_documents"].c.id == source_id
            )
        ).mappings().first()
        if source is None:
            continue
        items = list(_json(source["content_json"]).get("items", []))
        if _compatible(items, snapshot, content):
            candidates.append(
                (
                    -_match_score(items, snapshot),
                    int(package["version"]),
                    package,
                    items,
                )
            )
    if candidates:
        _, _, package, items = min(candidates, key=lambda item: (item[0], item[1]))
        return package, items
    return _build_legacy_package(
        connection,
        tables,
        session=session,
        snapshot=snapshot,
        created_at=created_at,
    )


def upgrade() -> None:
    connection = op.get_bind()
    metadata = sa.MetaData()
    metadata.reflect(
        bind=connection,
        only=[
            "sessions",
            "processed_scripts",
            "result_packages",
            "package_documents",
            "derived_artifacts",
        ],
    )
    tables = metadata.tables
    scripts = connection.execute(
        sa.select(tables["processed_scripts"]).order_by(
            tables["processed_scripts"].c.session_id,
            tables["processed_scripts"].c.version,
        )
    ).mappings()
    for script in scripts:
        if connection.scalar(
            sa.select(sa.func.count())
            .select_from(tables["derived_artifacts"])
            .where(tables["derived_artifacts"].c.id == script["id"])
        ):
            continue
        session = connection.execute(
            sa.select(tables["sessions"]).where(
                tables["sessions"].c.id == script["session_id"]
            )
        ).mappings().one()
        snapshot = list(_json(script["source_segment_snapshot"]))
        content = dict(_json(script["content_json"]))
        package, source_items = _find_or_create_package(
            connection,
            tables,
            session=session,
            snapshot=snapshot,
            content=content,
            created_at=script["created_at"],
        )
        migrated_sections: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []
        for index, section_value in enumerate(content["sections"]):
            section = dict(section_value)
            section_segments = {
                str(value) for value in section["source_segment_ids"]
            }
            referenced = [
                item
                for item in source_items
                if set(item["source_segment_ids"]) <= section_segments
            ]
            source_item_ids = [str(item["item_id"]) for item in referenced]
            section["source_item_ids"] = source_item_ids
            migrated_sections.append(section)
            evidence.append(
                {
                    "evidence_key": f"section:{index}",
                    "source_item_ids": source_item_ids,
                    "source_segment_ids": list(
                        dict.fromkeys(
                            segment_id
                            for item in referenced
                            for segment_id in item["source_segment_ids"]
                        )
                    ),
                    "start_ms": min(int(item["start_ms"]) for item in referenced),
                    "end_ms": max(int(item["end_ms"]) for item in referenced),
                }
            )
        migrated_content = {
            **content,
            "sections": migrated_sections,
            "legacy_source_segment_snapshot": snapshot,
            "legacy_markdown_text": script["markdown_text"],
        }
        connection.execute(
            sa.insert(tables["derived_artifacts"]).values(
                id=script["id"],
                package_id=package["id"],
                package_version=package["version"],
                package_content_hash=package["content_hash"],
                artifact_kind="clean_script",
                artifact_version=script["version"],
                target_language=None,
                status="generated",
                provider=script["provider"],
                model=script["model"],
                workflow_version=_WORKFLOW_VERSION,
                options_json={
                    "migrated_from": "processed_scripts",
                    "legacy_script_id": script["id"],
                    "legacy_script_version": script["version"],
                },
                content_json=migrated_content,
                evidence_json=evidence,
                parent_artifact_id=None,
                created_by="model",
                created_at=script["created_at"],
                approved_at=None,
            )
        )


def downgrade() -> None:
    connection = op.get_bind()
    metadata = sa.MetaData()
    metadata.reflect(
        bind=connection,
        only=[
            "processed_scripts",
            "result_packages",
            "package_documents",
            "derived_artifacts",
        ],
    )
    tables = metadata.tables
    scripts = list(
        connection.execute(
            sa.select(
                tables["processed_scripts"].c.id,
                tables["processed_scripts"].c.session_id,
                tables["processed_scripts"].c.source_segment_snapshot,
            )
        ).mappings()
    )
    connection.execute(
        sa.delete(tables["derived_artifacts"]).where(
            tables["derived_artifacts"].c.workflow_version == _WORKFLOW_VERSION
        )
    )
    package_ids = [
        _legacy_package_id(
            str(script["session_id"]),
            list(_json(script["source_segment_snapshot"])),
        )
        for script in scripts
    ]
    if package_ids:
        connection.execute(
            sa.delete(tables["package_documents"]).where(
                tables["package_documents"].c.package_id.in_(package_ids)
            )
        )
        connection.execute(
            sa.delete(tables["result_packages"]).where(
                tables["result_packages"].c.id.in_(package_ids)
            )
        )
