from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
from livekit import rtc


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIVE_CAPTION_TOPIC = "livecaption.events.v1"


async def run_replay(
    *,
    input_path: Path,
    api_base_url: str,
    session_id: str,
) -> dict[str, object]:
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "run_demo_replay.py"),
        "--file",
        str(input_path),
        "--api",
        api_base_url,
        "--session-id",
        session_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Replay failed with exit code {process.returncode}: {detail}"
        )
    lines = stdout.decode("utf-8", errors="replace").splitlines()
    if not lines:
        raise RuntimeError("Replay produced no JSON output")
    result = json.loads(lines[-1])
    if not isinstance(result, dict):
        raise RuntimeError("Replay result must be an object")
    return result


async def verify(
    *,
    input_path: Path,
    api_base_url: str,
    session_id: str,
    require_browser_participant: bool = False,
) -> dict[str, object]:
    async with httpx.AsyncClient(
        base_url=api_base_url,
        timeout=20.0,
        trust_env=False,
    ) as client:
        session_response = await client.get(f"/api/sessions/{session_id}")
        session_response.raise_for_status()
        token_response = await client.post(
            "/api/livekit/token",
            json={
                "session_id": session_id,
                "participant_identity": f"stage3-observer-{session_id[:8]}",
            },
        )
        token_response.raise_for_status()
        token = token_response.json()

        observer = rtc.Room()
        worker_ready = asyncio.Event()
        browser_ready = asyncio.Event()
        completed_received = asyncio.Event()
        events: list[dict[str, object]] = []
        browser_participant_identity: str | None = None

        @observer.on("participant_connected")
        def on_participant_connected(participant: rtc.RemoteParticipant) -> None:
            nonlocal browser_participant_identity
            if participant.identity.startswith("agent-"):
                worker_ready.set()
            if participant.identity.startswith("browser-"):
                browser_participant_identity = participant.identity
                browser_ready.set()

        @observer.on("data_received")
        def on_data_received(packet: rtc.DataPacket) -> None:
            if packet.topic != LIVE_CAPTION_TOPIC:
                return
            try:
                decoded = json.loads(packet.data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return
            if (
                not isinstance(decoded, dict)
                or decoded.get("schema_version") != 1
                or decoded.get("session_id") != session_id
            ):
                return
            events.append(decoded)
            if (
                decoded.get("type") == "session.status"
                and isinstance(decoded.get("payload"), dict)
                and decoded["payload"].get("status") == "completed"
            ):
                completed_received.set()

        replay_result: dict[str, object] | None = None
        unknown_packet_sent = False
        try:
            await observer.connect(token["url"], token["token"])
            for participant in observer.remote_participants.values():
                if participant.identity.startswith("agent-"):
                    worker_ready.set()
                if participant.identity.startswith("browser-"):
                    browser_participant_identity = participant.identity
                    browser_ready.set()
            await asyncio.wait_for(worker_ready.wait(), timeout=20.0)
            if require_browser_participant:
                try:
                    await asyncio.wait_for(browser_ready.wait(), timeout=20.0)
                except TimeoutError:
                    raise RuntimeError(
                        "No browser-* participant joined the target Session room"
                    ) from None

            await observer.local_participant.publish_data(
                b'{"schema_version":999,"type":"unknown"}',
                reliable=True,
                topic=LIVE_CAPTION_TOPIC,
            )
            unknown_packet_sent = True
            replay_result = await run_replay(
                input_path=input_path,
                api_base_url=api_base_url,
                session_id=session_id,
            )
            await asyncio.wait_for(completed_received.wait(), timeout=45.0)

            deadline = asyncio.get_running_loop().time() + 10.0
            while True:
                refreshed_session = (
                    await client.get(f"/api/sessions/{session_id}")
                ).json()
                segments = (
                    await client.get(f"/api/sessions/{session_id}/segments")
                ).json()
                if refreshed_session.get("status") == "completed" and segments:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.2)
        finally:
            await observer.disconnect()

    caption_events = [
        item for item in events if item.get("type") == "caption.upsert"
    ]
    draft_events = [
        item for item in caption_events
        if isinstance(item.get("payload"), dict)
        and item["payload"].get("status") == "draft"
    ]
    final_events = [
        item for item in caption_events
        if isinstance(item.get("payload"), dict)
        and item["payload"].get("status") == "final"
    ]
    metrics_events = [
        item for item in events if item.get("type") == "session.metrics"
    ]
    state_sequence: list[str] = []
    for item in events:
        if item.get("type") != "session.status":
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
            continue
        status = payload["status"]
        if not state_sequence or state_sequence[-1] != status:
            state_sequence.append(status)
    expected_state_sequence = [
        "running",
        "running",
        "finalizing",
        "completed",
    ]
    partial_replaced = (
        len(draft_events) >= 2
        and len(
            {
                item["payload"].get("segment_id")
                for item in draft_events
                if isinstance(item.get("payload"), dict)
            }
        ) == 1
        and len(
            {
                item["payload"].get("text")
                for item in draft_events
                if isinstance(item.get("payload"), dict)
            }
        ) >= 2
    )
    success = (
        partial_replaced
        and len(final_events) == 1
        and len(segments) == 1
        and refreshed_session.get("status") == "completed"
        and len(metrics_events) == 1
        and unknown_packet_sent
        and state_sequence == expected_state_sequence
    )
    return {
        "status": "ok" if success else "failed",
        "session_id": session_id,
        "room_name": token["room_name"],
        "partial_event_count": len(draft_events),
        "partial_replaced": partial_replaced,
        "final_event_count": len(final_events),
        "snapshot_final_count": len(segments),
        "final_text": segments[0]["display_text"] if segments else None,
        "session_status": refreshed_session.get("status"),
        "state_sequence": state_sequence,
        "expected_state_sequence": expected_state_sequence,
        "metrics_event_count": len(metrics_events),
        "unknown_packet_sent": unknown_packet_sent,
        "browser_participant_required": require_browser_participant,
        "browser_participant_identity": browser_participant_identity,
        "replay_frame_count": (
            replay_result.get("frame_count") if replay_result else None
        ),
        "clean_shutdown": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run real Stage 3 Replay, caption event, and snapshot acceptance."
    )
    parser.add_argument("--file", required=True, type=Path, dest="input_path")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--require-browser-participant",
        action="store_true",
        help=(
            "Require an already-connected browser-* participant in the same "
            "Room before Replay starts"
        ),
    )
    args = parser.parse_args()
    input_path = args.input_path.resolve()
    if not input_path.is_file():
        parser.error(f"audio file does not exist: {input_path}")

    result = asyncio.run(
        verify(
            input_path=input_path,
            api_base_url=args.api.rstrip("/"),
            session_id=args.session_id,
            require_browser_participant=args.require_browser_participant,
        )
    )
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
