from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient

from app.main import create_app
from app.persistence.database import Database
from app.settings import get_settings


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def resolve_database_url(database_url: str) -> str:
    relative_prefix = "sqlite:///./"
    if database_url.startswith(relative_prefix):
        filename = database_url.removeprefix(relative_prefix)
        return f"sqlite:///{(BACKEND / filename).resolve().as_posix()}"
    return database_url


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify one real DeepSeek processed-script generation."
    )
    parser.add_argument(
        "--session-id",
        help="Completed Session with durable Final captions",
    )
    return parser.parse_args()


def source_exports(
    client: TestClient,
    session_id: str,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for export_format in ("json", "srt", "vtt", "markdown"):
        response = client.get(
            f"/api/sessions/{session_id}/export",
            params={"format": export_format},
        )
        ensure(response.status_code == 200, f"{export_format} export failed")
        result[export_format] = hashlib.sha256(response.content).hexdigest()
    return result


def choose_session(client: TestClient, requested: str | None) -> str:
    if requested:
        snapshot = client.get(f"/api/sessions/{requested}/segments")
        ensure(snapshot.status_code == 200, "requested Session was not found")
        ensure(bool(snapshot.json()), "requested Session has no Final captions")
        return requested

    history = client.get("/api/sessions")
    ensure(history.status_code == 200, "Session history failed")
    for item in history.json():
        snapshot = client.get(f"/api/sessions/{item['id']}/segments")
        if snapshot.status_code == 200 and snapshot.json():
            return str(item["id"])
    raise RuntimeError("no Session with Final captions is available")


def verify(session_id: str | None) -> dict[str, object]:
    settings = get_settings()
    ensure(bool(settings.deepseek_api_key), "DEEPSEEK_API_KEY is not configured")
    database = Database(resolve_database_url(settings.database_url))
    with TestClient(create_app(settings=settings, database=database)) as client:
        selected_session_id = choose_session(client, session_id)
        snapshot_before = client.get(
            f"/api/sessions/{selected_session_id}/segments"
        )
        snapshot_before.raise_for_status()
        source_before = snapshot_before.json()
        hashes_before = source_exports(client, selected_session_id)

        generated = client.post(
            f"/api/sessions/{selected_session_id}/scripts"
        )
        ensure(
            generated.status_code == 201,
            f"DeepSeek generation failed with HTTP {generated.status_code}",
        )
        script = generated.json()
        detail = client.get(f"/api/scripts/{script['id']}")
        exported = client.get(f"/api/scripts/{script['id']}/export")
        history = client.get(
            f"/api/sessions/{selected_session_id}/scripts"
        )
        snapshot_after = client.get(
            f"/api/sessions/{selected_session_id}/segments"
        )
        ensure(detail.status_code == 200, "script detail failed")
        ensure(exported.status_code == 200, "script export failed")
        ensure(history.status_code == 200, "script history failed")
        ensure(snapshot_after.status_code == 200, "source snapshot failed")

        referenced_ids = [
            segment_id
            for section in script["content"]["sections"]
            for segment_id in section["source_segment_ids"]
        ]
        source_ids = [item["segment_id"] for item in source_before]
        ensure(
            sorted(referenced_ids) == sorted(source_ids),
            "processed script source references do not match Final snapshot",
        )
        ensure(
            snapshot_after.json() == source_before,
            "DeepSeek generation changed source Final captions",
        )
        ensure(
            source_exports(client, selected_session_id) == hashes_before,
            "DeepSeek generation changed source exports",
        )
        ensure(
            history.json()[0]["id"] == script["id"],
            "new script is not the latest version",
        )
        ensure(
            exported.headers["content-type"].startswith("text/markdown"),
            "processed export is not Markdown",
        )
        ensure(
            all(segment_id in exported.text for segment_id in source_ids),
            "processed Markdown lacks source traceability",
        )

    return {
        "status": "ok",
        "session_id": selected_session_id,
        "script_id": script["id"],
        "version": script["version"],
        "provider": script["provider"],
        "model": script["model"],
        "source_segment_count": len(source_ids),
        "section_count": len(script["content"]["sections"]),
        "source_references_complete": True,
        "source_rows_unchanged": True,
        "source_exports_unchanged": True,
        "markdown_exported": True,
        "clean_shutdown": True,
    }


def main() -> None:
    result = verify(parse_args().session_id)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
