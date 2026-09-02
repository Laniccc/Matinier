from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.logging import configure_logging  # noqa: E402
from app.replay.clock import ReplayClock  # noqa: E402
from app.replay.decoder import FFmpegPCMDecoder  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.transcription.bailian import (  # noqa: E402
    BailianConfig,
    BailianSpeechRecognitionProvider,
)
from app.transcription.errors import ASRError  # noqa: E402
from app.transcription.session import TranscriptionSession  # noqa: E402


async def verify(input_path: Path, settings: Settings) -> dict[str, object]:
    decoder = FFmpegPCMDecoder(input_path)
    replay_clock = ReplayClock()

    def provider_factory() -> BailianSpeechRecognitionProvider:
        return BailianSpeechRecognitionProvider(
            BailianConfig(
                api_key=settings.dashscope_api_key,
                workspace_id=settings.dashscope_workspace_id,
                region=settings.dashscope_region,
                model=settings.bailian_asr_model,
                websocket_url=settings.dashscope_websocket_url,
                start_timeout_seconds=settings.asr_start_timeout_seconds,
                finish_timeout_seconds=settings.asr_finish_timeout_seconds,
            )
        )

    session = TranscriptionSession(
        provider_factory=provider_factory,
        queue_max_chunks=settings.asr_queue_max_chunks,
        chunk_duration_ms=settings.asr_chunk_duration_ms,
        startup_retries=settings.asr_startup_retries,
        log_context={
            "session_id": "stage2-real-bailian-verification",
            "room_name": "direct-provider-verification",
            "participant_identity": "local-verifier",
        },
    )
    frame_count = 0
    started_at = time.monotonic()
    try:
        await session.start()
        async for pcm_frame in decoder.frames():
            await replay_clock.wait_for_frame(pcm_frame.duration_seconds)
            session.send_frame(pcm_frame.data)
            frame_count += 1
        await replay_clock.wait_until_complete()
        metrics = await session.finish()
    finally:
        await session.aclose()

    await asyncio.sleep(0)
    active_names = {
        task.get_name()
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    }
    clean_shutdown = decoder.active_process is None and not any(
        name.startswith(("transcription-", "bailian-asr-"))
        for name in active_names
    )
    partial_received = metrics.first_partial_latency_ms is not None
    status = (
        "ok"
        if partial_received
        and metrics.final_result_count > 0
        and metrics.provider_error_count == 0
        and clean_shutdown
        else "failed"
    )
    return {
        "status": status,
        "frame_count": frame_count,
        "audio_duration_seconds": round(replay_clock.audio_elapsed, 3),
        "elapsed_seconds": round(time.monotonic() - started_at, 3),
        "partial_received": partial_received,
        "final_result_count": metrics.final_result_count,
        "first_partial_latency_ms": metrics.first_partial_latency_ms,
        "average_final_latency_ms": metrics.average_final_latency_ms,
        "provider_error_count": metrics.provider_error_count,
        "sent_audio_chunk_count": metrics.sent_audio_chunk_count,
        "sent_audio_bytes": metrics.sent_audio_bytes,
        "clean_shutdown": clean_shutdown,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify Stage 2 against the configured real Bailian ASR service."
    )
    parser.add_argument("--file", required=True, type=Path, dest="input_path")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    input_path = args.input_path.resolve()
    if not input_path.is_file():
        parser.error(f"audio file does not exist: {input_path}")

    configure_logging("stage2-bailian-verifier", args.log_level.upper())
    settings = Settings()
    try:
        result = asyncio.run(verify(input_path, settings))
    except ASRError as error:
        result = {
            "status": "failed",
            "error_type": type(error).__name__,
            "error_code": error.code,
        }
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
