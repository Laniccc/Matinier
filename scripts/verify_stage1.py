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
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.replay.decoder import FFmpegPCMDecoder  # noqa: E402
from app.replay.source import ReplayResult, ReplaySource  # noqa: E402


async def run_external_replay_cli(
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
        raise RuntimeError(f"Replay CLI failed with exit code {process.returncode}: {detail}")

    output_lines = stdout.decode("utf-8", errors="replace").splitlines()
    if not output_lines:
        raise RuntimeError("Replay CLI produced no JSON result")
    result = json.loads(output_lines[-1])
    if not isinstance(result, dict) or result.get("session_id") != session_id:
        raise RuntimeError("Replay CLI did not reuse the requested Session")
    return result


async def create_credentials(api_base_url: str, input_path: Path) -> dict[str, object]:
    async with httpx.AsyncClient(
        base_url=api_base_url,
        timeout=15.0,
        trust_env=False,
    ) as client:
        session_response = await client.post(
            "/api/sessions",
            json={
                "source_type": "file",
                "source_name": input_path.name,
                "language": "zh-CN",
            },
        )
        session_response.raise_for_status()
        session = session_response.json()

        viewer_response = await client.post(
            "/api/livekit/token",
            json={
                "session_id": session["id"],
                "participant_identity": f"stage1-observer-{session['id'][:8]}",
            },
        )
        viewer_response.raise_for_status()

        replay_response = await client.post(
            "/api/livekit/token",
            json={"session_id": session["id"], "participant_type": "replay"},
        )
        replay_response.raise_for_status()
        return {
            "session": session,
            "viewer": viewer_response.json(),
            "replay": replay_response.json(),
        }


async def verify(
    *,
    input_path: Path,
    api_base_url: str,
    cancel_after: float | None,
    external_cli: bool = False,
) -> dict[str, object]:
    credentials = await create_credentials(api_base_url, input_path)
    session = credentials["session"]
    viewer = credentials["viewer"]
    replay_token = credentials["replay"]
    assert isinstance(session, dict)
    assert isinstance(viewer, dict)
    assert isinstance(replay_token, dict)

    expected_identity = f"replay-{session['id']}"
    observer = rtc.Room()
    worker_ready = asyncio.Event()
    replay_participant_seen = asyncio.Event()
    replay_track_seen = asyncio.Event()
    replay_track_unpublished = asyncio.Event()

    @observer.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant) -> None:
        if participant.identity.startswith("agent-"):
            worker_ready.set()
        if participant.identity == expected_identity:
            replay_participant_seen.set()

    @observer.on("track_published")
    def on_track_published(
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if participant.identity == expected_identity and publication.name == "replay-audio":
            replay_track_seen.set()

    @observer.on("track_unpublished")
    def on_track_unpublished(
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if participant.identity == expected_identity and publication.name == "replay-audio":
            replay_track_unpublished.set()

    decoder = FFmpegPCMDecoder(input_path)
    replay = ReplaySource(input_path, decoder=decoder, subscription_timeout=15.0)
    result: ReplayResult | None = None
    cli_result: dict[str, object] | None = None
    cancelled = False
    replay_task: asyncio.Task[ReplayResult | dict[str, object]] | None = None

    try:
        await observer.connect(viewer["url"], viewer["token"])
        for participant in observer.remote_participants.values():
            if participant.identity.startswith("agent-"):
                worker_ready.set()
        await asyncio.wait_for(worker_ready.wait(), timeout=15.0)

        if external_cli:
            replay_task = asyncio.create_task(
                run_external_replay_cli(
                    input_path=input_path,
                    api_base_url=api_base_url,
                    session_id=str(session["id"]),
                ),
                name="stage1-external-cli-replay",
            )
        else:
            replay_task = asyncio.create_task(
                replay.run(replay_token["url"], replay_token["token"]),
                name="stage1-verification-replay",
            )
        await asyncio.wait_for(replay_participant_seen.wait(), timeout=10.0)
        await asyncio.wait_for(replay_track_seen.wait(), timeout=10.0)

        if cancel_after is None:
            completed = await replay_task
            if isinstance(completed, ReplayResult):
                result = completed
            else:
                cli_result = completed
        else:
            await asyncio.sleep(cancel_after)
            replay_task.cancel()
            try:
                await replay_task
            except asyncio.CancelledError:
                cancelled = True

        await asyncio.wait_for(replay_track_unpublished.wait(), timeout=5.0)
    finally:
        if replay_task is not None and not replay_task.done():
            replay_task.cancel()
            await asyncio.gather(replay_task, return_exceptions=True)
        await observer.disconnect()

    return {
        "status": "cancelled" if cancelled else "completed",
        "session_id": session["id"],
        "room_name": session["room_name"],
        "replay_participant_seen": replay_participant_seen.is_set(),
        "replay_track_seen": replay_track_seen.is_set(),
        "replay_track_unpublished": replay_track_unpublished.is_set(),
        "participant_identity": expected_identity,
        "track_name": "replay-audio",
        "external_cli_session_reuse": external_cli,
        "frame_count": (
            result.frame_count
            if result is not None
            else cli_result.get("frame_count") if cli_result is not None else None
        ),
        "audio_duration_seconds": (
            result.audio_duration_seconds
            if result is not None
            else cli_result.get("audio_duration_seconds") if cli_result is not None else None
        ),
        "playback_elapsed_seconds": (
            result.playback_elapsed_seconds
            if result is not None
            else cli_result.get("playback_elapsed_seconds") if cli_result is not None else None
        ),
        "ffmpeg_process_active": (
            None if external_cli else decoder.active_process is not None
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify Stage 1 with an RTC viewer.")
    parser.add_argument("--file", required=True, type=Path, dest="input_path")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--cancel-after", type=float)
    parser.add_argument(
        "--external-cli",
        action="store_true",
        help="Run the real replay CLI as a child process with the observer Session ID.",
    )
    args = parser.parse_args()

    input_path = args.input_path.resolve()
    if not input_path.is_file():
        parser.error(f"audio file does not exist: {input_path}")
    if args.cancel_after is not None and args.cancel_after <= 0:
        parser.error("--cancel-after must be positive")
    if args.external_cli and args.cancel_after is not None:
        parser.error("--external-cli cannot be combined with --cancel-after")

    result = asyncio.run(
        verify(
            input_path=input_path,
            api_base_url=args.api.rstrip("/"),
            cancel_after=args.cancel_after,
            external_cli=args.external_cli,
        )
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
