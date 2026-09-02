from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.replay.source import ReplaySource  # noqa: E402


async def verify(
    *,
    api_base_url: str,
    input_path: Path,
    room_id: str | None,
) -> dict[str, object]:
    async with httpx.AsyncClient(
        base_url=api_base_url.rstrip("/"),
        timeout=20,
        trust_env=False,
    ) as client:
        if room_id is None:
            rooms_response = await client.get("/api/rooms")
            rooms_response.raise_for_status()
            rooms = [
                room
                for room in rooms_response.json()
                if room.get("status") == "ready"
            ]
            if rooms:
                room = rooms[0]
            else:
                created = await client.post(
                    "/api/rooms",
                    json={"display_name": "Room 输入验证"},
                )
                created.raise_for_status()
                room = created.json()
        else:
            room_response = await client.get(f"/api/rooms/{room_id}")
            room_response.raise_for_status()
            room = room_response.json()

        run_response = await client.post(
            f"/api/rooms/{room['id']}/caption-runs",
            json={
                "source_type": "file",
                "source_name": input_path.name,
                "language": "zh-CN",
            },
        )
        run_response.raise_for_status()
        run = run_response.json()
        token_response = await client.post(
            f"/api/rooms/{room['id']}/token",
            json={
                "participant_identity": (
                    f"room-input-verifier-{uuid.uuid4().hex[:8]}"
                )
            },
        )
        token_response.raise_for_status()
        token = token_response.json()

    source = ReplaySource(
        input_path,
        track_name=f"caption-input-{run['id']}",
    )
    result = await source.run(token["url"], token["token"])

    deadline = time.monotonic() + 30
    latest = run
    async with httpx.AsyncClient(
        base_url=api_base_url.rstrip("/"),
        timeout=10,
        trust_env=False,
    ) as client:
        while time.monotonic() < deadline:
            response = await client.get(f"/api/sessions/{run['id']}")
            response.raise_for_status()
            latest = response.json()
            if latest["status"] in {"completed", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.5)

    if latest["status"] != "completed":
        raise RuntimeError(
            f"Managed Room input did not complete: {latest['status']}"
        )
    return {
        "status": latest["status"],
        "room_id": room["id"],
        "room_name": room["room_name"],
        "session_id": run["id"],
        "track_name": result.track_name,
        "frame_count": result.frame_count,
        "audio_duration_seconds": round(result.audio_duration_seconds, 3),
        "worker_remains_in_room": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish a file through the managed Room input track contract.",
    )
    parser.add_argument("--file", required=True, type=Path, dest="input_path")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--room-id")
    args = parser.parse_args()
    input_path = args.input_path.resolve()
    if not input_path.is_file():
        parser.error(f"audio file does not exist: {input_path}")
    print(
        json.dumps(
            asyncio.run(
                verify(
                    api_base_url=args.api,
                    input_path=input_path,
                    room_id=args.room_id,
                )
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
