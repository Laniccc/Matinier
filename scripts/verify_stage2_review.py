from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.artifacts import ArtifactEvidence, ArtifactRepository  # noqa: E402
from app.main import create_app  # noqa: E402
from app.packages import PackageBuilder, PackageRepository  # noqa: E402
from app.persistence.database import Database  # noqa: E402
from app.persistence.models import SegmentRecord, SessionRecord  # noqa: E402
from app.settings import Settings  # noqa: E402


NOW = dt.datetime(2026, 8, 10, 12, tzinfo=dt.UTC)


def _seed_session(db_session) -> str:
    session_id = "stage2f-review-session"
    db_session.add(
        SessionRecord(
            id=session_id,
            room_name="stage2f-review-room",
            status="completed",
            source_type="file",
            source_name="stage2f-review.wav",
            language="zh-CN",
            final_result_count=1,
            created_at=NOW,
            started_at=NOW,
            ended_at=NOW + dt.timedelta(seconds=2),
        )
    )
    db_session.flush()
    db_session.add(
        SegmentRecord(
            id="stage2f-source-row",
            session_id=session_id,
            segment_id="stage2f-source-segment",
            track_id="stage2f-track",
            revision=2,
            language="zh-CN",
            raw_text="阶段二审核验收",
            display_text="阶段二审核验收。",
            audio_start_ms=100,
            audio_end_ms=1_200,
            confidence=0.97,
            status="final",
            received_at_ms=int(NOW.timestamp() * 1_000),
            finalized_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    db_session.flush()
    return session_id


def _create_clean(db_session, package, text: str):
    source = next(
        document
        for document in package.documents
        if document.document_id
        == package.manifest.effective_source_document_id
    )
    item = source.content.items[0]
    return ArtifactRepository(db_session).create_generated(
        package=package,
        artifact_kind="clean_script",
        target_language=None,
        provider="fake",
        model="fake-stage2f",
        workflow_version="stage2f-verifier-v1",
        options={},
        content={
            "title": "Stage 2F model transcript",
            "sections": [
                {
                    "source_item_ids": [item.item_id],
                    "source_segment_ids": list(item.source_segment_ids),
                    "start_ms": item.start_ms,
                    "end_ms": item.end_ms,
                    "clean_text": text,
                    "notes": [],
                }
            ],
            "warnings": [],
        },
        evidence=(
            ArtifactEvidence(
                evidence_key="section:0",
                source_item_ids=(item.item_id,),
                source_segment_ids=item.source_segment_ids,
                start_ms=item.start_ms,
                end_ms=item.end_ms,
            ),
        ),
    )


def _require(response, expected: int, label: str) -> dict[str, object]:
    if response.status_code != expected:
        raise RuntimeError(
            f"{label} returned {response.status_code}: {response.text}"
        )
    return response.json()


def verify() -> dict[str, object]:
    with TemporaryDirectory(prefix="matinier-stage2f-") as temp_dir:
        data_dir = Path(temp_dir)
        settings = Settings(
            _env_file=None,
            app_env="test",
            livekit_url="ws://127.0.0.1:7880",
            livekit_api_key="test-key",
            livekit_api_secret="test-secret-that-is-at-least-32-bytes",
            livekit_room_name="stage2f-review-room",
            database_url="sqlite://",
            data_dir=data_dir,
            dashscope_api_key=None,
            dashscope_workspace_id=None,
            deepseek_api_key=None,
            log_level="WARNING",
        )
        database = Database(settings.database_url)
        database.create_schema()
        with database.session() as db_session:
            session_id = _seed_session(db_session)
            package_v1 = PackageBuilder(db_session).build_baseline(session_id)
            model_v1 = _create_clean(
                db_session,
                package_v1,
                "模型生成的台本。",
            )
            package_v2 = PackageBuilder(db_session).build_baseline(session_id)
            other_package_artifact = _create_clean(
                db_session,
                package_v2,
                "新 Package 的独立台本。",
            )
            package_v1_hash = package_v1.content_hash
            stage1_before = tuple(
                (
                    item.segment_id,
                    item.revision,
                    item.display_text,
                    item.status,
                )
                for item in db_session.query(SegmentRecord)
                .filter(SegmentRecord.session_id == session_id)
                .all()
            )
            db_session.commit()

        application = create_app(settings=settings, database=database)
        with TestClient(application) as client:
            original = _require(
                client.get(f"/api/artifacts/{model_v1.artifact_id}"),
                200,
                "read old-Package Artifact",
            )
            edited_content = dict(original["content"])
            edited_content["sections"] = [
                {
                    **original["content"]["sections"][0],
                    "clean_text": "人工审核后的台本。",
                }
            ]
            human_v2 = _require(
                client.post(
                    f"/api/artifacts/{model_v1.artifact_id}/versions",
                    json={"content": edited_content},
                ),
                201,
                "create human Artifact version",
            )
            _require(
                client.post(f"/api/artifacts/{model_v1.artifact_id}/approve"),
                200,
                "approve model Artifact",
            )
            approved_v2 = _require(
                client.post(
                    f"/api/artifacts/{human_v2['artifact_id']}/approve"
                ),
                200,
                "approve human Artifact",
            )
            history = _require(
                client.get(
                    f"/api/artifacts/{human_v2['artifact_id']}/history"
                ),
                200,
                "read Artifact history",
            )
            approved = _require(
                client.get(
                    f"/api/packages/{package_v1.package_id}/approved-artifacts"
                ),
                200,
                "read approved Artifacts",
            )
            old_model = _require(
                client.get(f"/api/artifacts/{model_v1.artifact_id}"),
                200,
                "reload superseded Artifact",
            )
            old_package_artifacts = _require(
                client.get(
                    f"/api/packages/{package_v1.package_id}/artifacts"
                ),
                200,
                "read old Package Artifact list",
            )
            exported = client.get(
                f"/api/artifacts/{human_v2['artifact_id']}/export?format=json"
            )
            if exported.status_code != 200:
                raise RuntimeError(
                    f"Artifact JSON export returned {exported.status_code}"
                )

            with database.session() as db_session:
                reloaded_package = PackageRepository(db_session).load(
                    package_v1.package_id
                )
                stage1_after = tuple(
                    (
                        item.segment_id,
                        item.revision,
                        item.display_text,
                        item.status,
                    )
                    for item in db_session.query(SegmentRecord)
                    .filter(SegmentRecord.session_id == session_id)
                    .all()
                )

            history_ids = {str(item["artifact_id"]) for item in history}
            if history_ids != {
                model_v1.artifact_id,
                str(human_v2["artifact_id"]),
            }:
                raise RuntimeError("Artifact identity history is not isolated")
            if other_package_artifact.artifact_id in history_ids:
                raise RuntimeError("Artifact history crossed Package boundary")
            if old_model["status"] != "superseded":
                raise RuntimeError("Old approved Artifact was not superseded")
            if [item["artifact_id"] for item in approved] != [
                human_v2["artifact_id"]
            ]:
                raise RuntimeError("Current approved Artifact is not unique")
            if reloaded_package.content_hash != package_v1_hash:
                raise RuntimeError("Old Package content hash changed")
            if stage1_after != stage1_before:
                raise RuntimeError("Stage 1 Final evidence changed")

        return {
            "status": "ok",
            "session_id": session_id,
            "package_versions": [
                package_v1.package_version,
                package_v2.package_version,
            ],
            "artifact_identity": human_v2["identity_key"],
            "artifact_versions": [
                item["artifact_version"] for item in history
            ],
            "parent_chain_valid": (
                human_v2["parent_artifact_id"] == model_v1.artifact_id
            ),
            "approved_artifact_id": approved_v2["artifact_id"],
            "old_approved_superseded": True,
            "approved_unique": True,
            "cross_package_isolated": True,
            "old_package_artifact_count": len(old_package_artifacts),
            "old_package_hash_unchanged": True,
            "stage1_final_unchanged": True,
            "json_export_bytes": len(exported.content),
            "cloud_calls": 0,
        }


def main() -> int:
    try:
        result = verify()
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
