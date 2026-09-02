from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.logging import configure_logging  # noqa: E402
from app.replay.decoder import FFmpegDecodeError  # noqa: E402
from app.replay.source import ReplayLiveKitError, ReplaySource  # noqa: E402


async def resolve_session(
    client: httpx.AsyncClient,
    *,
    input_path: Path,
    language: str,
    session_id: str | None,
) -> dict[str, object]:
    if session_id is None:
        response = await client.post(
            "/api/sessions",
            json={
                "source_type": "file",
                "source_name": input_path.name,
                "language": language,
            },
        )
    else:
        response = await client.get(f"/api/sessions/{session_id}")

    response.raise_for_status()
    session = response.json()
    if not isinstance(session, dict):
        raise RuntimeError("Session API returned an invalid response")
    if session.get("source_type") != "file":
        raise RuntimeError("Replay requires a Session with source_type=file")
    if session_id is not None and session.get("id") != session_id:
        raise RuntimeError("Session API returned a mismatched Session ID")
    return session


async def run_demo(
    *,
    input_path: Path,
    api_base_url: str,
    language: str,
    sample_rate: int,
    frame_duration_ms: int,
    subscription_timeout: float,
    connect_timeout: float,
    ffmpeg_start_timeout: float,
    ffmpeg_path: str,
    session_id: str | None = None,
) -> dict[str, object]:
    async with httpx.AsyncClient(
        base_url=api_base_url.rstrip("/"),
        timeout=max(15.0, subscription_timeout),
        trust_env=False,
    ) as client:
        session = await resolve_session(
            client,
            input_path=input_path,
            language=language,
            session_id=session_id,
        )

        token_response = await client.post(
            "/api/livekit/token",
            json={
                "session_id": session["id"],
                "participant_type": "replay",
            },
        )
        token_response.raise_for_status()
        token = token_response.json()

    replay = ReplaySource(
        input_path,
        sample_rate=sample_rate,
        frame_duration_ms=frame_duration_ms,
        connect_timeout=connect_timeout,
        subscription_timeout=subscription_timeout,
        ffmpeg_start_timeout=ffmpeg_start_timeout,
        ffmpeg_path=ffmpeg_path,
    )
    wall_started_at = time.monotonic()
    try:
        result = await replay.run(token["url"], token["token"])
    except asyncio.CancelledError:
        await asyncio.shield(
            report_replay_terminal_status(
                api_base_url=api_base_url,
                session_id=str(session["id"]),
                status="cancelled",
            )
        )
        raise
    except FFmpegDecodeError:
        await report_replay_terminal_status(
            api_base_url=api_base_url,
            session_id=str(session["id"]),
            status="failed",
            error_code="media_decode_error",
        )
        raise
    except ReplayLiveKitError:
        await report_replay_terminal_status(
            api_base_url=api_base_url,
            session_id=str(session["id"]),
            status="failed",
            error_code="livekit_error",
        )
        raise
    wall_elapsed_seconds = time.monotonic() - wall_started_at
    return {
        "status": "completed",
        "session_id": session["id"],
        "room_name": session["room_name"],
        "participant_identity": result.participant_identity,
        "track_name": result.track_name,
        "frame_count": result.frame_count,
        "audio_duration_seconds": round(result.audio_duration_seconds, 6),
        "playback_elapsed_seconds": round(result.playback_elapsed_seconds, 6),
        "wall_elapsed_seconds": round(wall_elapsed_seconds, 6),
    }


async def report_replay_terminal_status(
    *,
    api_base_url: str,
    session_id: str,
    status: str,
    error_code: str | None = None,
) -> None:
    payload: dict[str, str] = {"status": status}
    if error_code is not None:
        payload["error_code"] = error_code
    try:
        async with httpx.AsyncClient(
            base_url=api_base_url.rstrip("/"),
            timeout=10.0,
            trust_env=False,
        ) as client:
            response = await client.post(
                f"/api/sessions/{session_id}/replay-status",
                json=payload,
            )
            response.raise_for_status()
    except httpx.HTTPError as error:
        logging.getLogger(__name__).error(
            "replay terminal status reporting failed",
            extra={
                "process_name": "replay",
                "session_id": session_id,
                "event": "replay_status_report_failed",
                "error_type": "persistence_error",
                "internal_error_type": type(error).__name__,
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay a WAV/MP3 file into LiveKit at real-time cadence.",
    )
    parser.add_argument("--file", required=True, type=Path, dest="input_path")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--language", default="zh-CN")
    parser.add_argument("--sample-rate", type=int, default=16_000)
    parser.add_argument("--frame-ms", type=int, default=20)
    parser.add_argument("--subscription-timeout", type=float, default=15.0)
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=float(os.getenv("LIVEKIT_CONNECT_TIMEOUT_SECONDS", "15")),
    )
    parser.add_argument(
        "--ffmpeg-start-timeout",
        type=float,
        default=float(os.getenv("FFMPEG_START_TIMEOUT_SECONDS", "10")),
    )
    parser.add_argument(
        "--ffmpeg-bin",
        default=os.getenv("FFMPEG_BIN", "ffmpeg"),
    )
    parser.add_argument(
        "--session-id",
        help="Reuse an existing file Session so an already-connected browser shares the Room.",
    )
    args = parser.parse_args()

    input_path = args.input_path.resolve()
    if not input_path.is_file():
        parser.error(f"audio file does not exist: {input_path}")

    configure_logging("replay", os.getenv("LOG_LEVEL", "INFO"))
    result = asyncio.run(
        run_demo(
            input_path=input_path,
            api_base_url=args.api,
            language=args.language,
            sample_rate=args.sample_rate,
            frame_duration_ms=args.frame_ms,
            subscription_timeout=args.subscription_timeout,
            connect_timeout=args.connect_timeout,
            ffmpeg_start_timeout=args.ffmpeg_start_timeout,
            ffmpeg_path=args.ffmpeg_bin,
            session_id=args.session_id,
        )
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
