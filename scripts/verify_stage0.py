from __future__ import annotations

import argparse
import asyncio
import json

import httpx
from livekit import rtc


async def verify(api_base_url: str, timeout_seconds: float) -> None:
    async with httpx.AsyncClient(base_url=api_base_url, timeout=timeout_seconds) as client:
        session_response = await client.post(
            "/api/sessions",
            json={"source_name": "stage-0-verification", "language": "zh-CN"},
        )
        session_response.raise_for_status()
        session = session_response.json()

        token_response = await client.post(
            "/api/livekit/token",
            json={
                "session_id": session["id"],
                "participant_identity": "stage-0-verifier",
            },
        )
        token_response.raise_for_status()
        token = token_response.json()

    room = rtc.Room()
    worker_joined = asyncio.Event()
    worker_identity: str | None = None

    @room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant) -> None:
        nonlocal worker_identity
        worker_identity = participant.identity
        worker_joined.set()

    try:
        await asyncio.wait_for(
            room.connect(token["url"], token["token"]),
            timeout=timeout_seconds,
        )
        for participant in room.remote_participants.values():
            worker_identity = participant.identity
            worker_joined.set()
            break

        await asyncio.wait_for(worker_joined.wait(), timeout=timeout_seconds)
        print(
            json.dumps(
                {
                    "status": "connected",
                    "session_id": session["id"],
                    "room_name": room.name,
                    "participant_identity": room.local_participant.identity,
                    "worker_identity": worker_identity,
                    "remote_participants": len(room.remote_participants),
                },
                ensure_ascii=False,
            )
        )
    finally:
        await room.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the Stage 0 LiveKit room path.")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()
    asyncio.run(verify(args.api.rstrip("/"), args.timeout))


if __name__ == "__main__":
    main()
