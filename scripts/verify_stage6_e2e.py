from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPORT_FORMATS = ("json", "srt", "vtt", "markdown")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Stage 6 real-service acceptance against already-running "
            "API, Worker, LiveKit, and frontend services."
        )
    )
    parser.add_argument(
        "--file",
        required=True,
        type=Path,
        dest="input_path",
        help="Local speech audio sent to real LiveKit and Bailian services",
    )
    parser.add_argument(
        "--api",
        default="http://127.0.0.1:8000",
        help="Already-running LiveCaption API base URL",
    )
    parser.add_argument(
        "--session-id",
        help=(
            "Fresh file Session to target; use the ID created by an already-"
            "connected page when --require-browser is enabled"
        ),
    )
    parser.add_argument(
        "--require-browser",
        action="store_true",
        help=(
            "Require a browser-* participant in the target Session Room before "
            "Replay starts; requires --session-id"
        ),
    )
    parser.add_argument(
        "--with-deepseek",
        action="store_true",
        help=(
            "Explicitly make one real DeepSeek request after ASR acceptance; "
            "without this option no DeepSeek request is made"
        ),
    )
    return parser.parse_args()


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def parse_last_json_line(stdout: bytes, *, label: str) -> dict[str, Any]:
    for line in reversed(stdout.decode("utf-8", errors="replace").splitlines()):
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict):
            return result
    raise RuntimeError(f"{label} produced no JSON object")


async def run_verifier(
    script_name: str,
    *arguments: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(PROJECT_ROOT / "scripts" / script_name),
        *arguments,
        cwd=str(PROJECT_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise RuntimeError(f"{script_name} timed out") from None
    if process.returncode != 0:
        raise RuntimeError(
            f"{script_name} failed with exit code {process.returncode}; "
            "inspect the local service logs"
        )
    result = parse_last_json_line(stdout, label=script_name)
    ensure(result.get("status") == "ok", f"{script_name} reported failure")
    return result


async def verify_exports(
    client: httpx.AsyncClient,
    session_id: str,
) -> dict[str, dict[str, object]]:
    results: dict[str, dict[str, object]] = {}
    for export_format in EXPORT_FORMATS:
        first = await client.get(
            f"/api/sessions/{session_id}/export",
            params={"format": export_format},
        )
        second = await client.get(
            f"/api/sessions/{session_id}/export",
            params={"format": export_format},
        )
        ensure(first.status_code == 200, f"{export_format} export failed")
        ensure(second.status_code == 200, f"{export_format} repeat export failed")
        ensure(bool(first.content), f"{export_format} export is empty")
        ensure(
            first.content == second.content,
            f"{export_format} export is not deterministic",
        )
        results[export_format] = {
            "byte_count": len(first.content),
            "sha256": hashlib.sha256(first.content).hexdigest(),
            "content_type": first.headers.get("content-type"),
            "deterministic": True,
        }
    return results


async def verify(
    *,
    input_path: Path,
    api_base_url: str,
    session_id: str | None,
    with_deepseek: bool,
    require_browser: bool,
) -> dict[str, object]:
    async with httpx.AsyncClient(
        base_url=api_base_url,
        timeout=30.0,
        trust_env=False,
    ) as client:
        health = await client.get("/health")
        ensure(health.status_code == 200, "API health check failed")

        if session_id is None:
            created = await client.post(
                "/api/sessions",
                json={
                    "source_type": "file",
                    "source_name": input_path.name,
                    "language": "zh-CN",
                },
            )
            ensure(created.status_code == 201, "file Session creation failed")
            session_id = str(created.json()["id"])
        else:
            selected = await client.get(f"/api/sessions/{session_id}")
            ensure(selected.status_code == 200, "requested Session was not found")
            selected_payload = selected.json()
            ensure(
                selected_payload.get("source_type") == "file",
                "requested Session is not a file Session",
            )
            ensure(
                selected_payload.get("status") in {"created", "starting"},
                "requested Session must be fresh and non-terminal",
            )

        stage3_arguments = [
            "--file",
            str(input_path),
            "--api",
            api_base_url,
            "--session-id",
            session_id,
        ]
        if require_browser:
            stage3_arguments.append("--require-browser-participant")
        stage3 = await run_verifier(
            "verify_stage3_e2e.py",
            *stage3_arguments,
            timeout_seconds=180.0,
        )

        session_response = await client.get(f"/api/sessions/{session_id}")
        segments_response = await client.get(
            f"/api/sessions/{session_id}/segments"
        )
        ensure(session_response.status_code == 200, "Session read failed")
        ensure(segments_response.status_code == 200, "Final snapshot read failed")
        session = session_response.json()
        segments = segments_response.json()
        ensure(session.get("status") == "completed", "Session did not complete")
        ensure(bool(segments), "Session has no durable Final captions")
        exports = await verify_exports(client, session_id)

    deepseek: dict[str, object]
    if with_deepseek:
        deepseek_result = await run_verifier(
            "verify_stage5_deepseek.py",
            "--session-id",
            session_id,
            timeout_seconds=180.0,
        )
        deepseek = {
            "requested": True,
            "status": deepseek_result.get("status"),
            "script_id": deepseek_result.get("script_id"),
            "version": deepseek_result.get("version"),
            "provider": deepseek_result.get("provider"),
            "model": deepseek_result.get("model"),
            "source_references_complete": deepseek_result.get(
                "source_references_complete"
            ),
            "source_exports_unchanged": deepseek_result.get(
                "source_exports_unchanged"
            ),
        }
    else:
        deepseek = {"requested": False, "status": "skipped"}

    expected_sequence = [
        "running",
        "running",
        "finalizing",
        "completed",
    ]
    ensure(
        stage3.get("state_sequence") == expected_sequence,
        "real-time Session state sequence did not match Stage 6",
    )
    return {
        "status": "ok",
        "session_id": session_id,
        "session_status": session.get("status"),
        "state_sequence": stage3.get("state_sequence"),
        "partial_event_count": stage3.get("partial_event_count"),
        "partial_replaced": stage3.get("partial_replaced"),
        "browser_participant_required": require_browser,
        "browser_participant_identity": stage3.get(
            "browser_participant_identity"
        ),
        "final_event_count": stage3.get("final_event_count"),
        "durable_final_count": len(segments),
        "metrics_event_count": stage3.get("metrics_event_count"),
        "replay_frame_count": stage3.get("replay_frame_count"),
        "exports": exports,
        "deepseek": deepseek,
        "clean_shutdown": bool(stage3.get("clean_shutdown")),
    }


def main() -> None:
    args = parse_args()
    input_path = args.input_path.resolve()
    if not input_path.is_file():
        raise SystemExit(f"audio file does not exist: {input_path}")
    if args.require_browser and not args.session_id:
        raise SystemExit("--require-browser requires --session-id")
    try:
        result = asyncio.run(
            verify(
                input_path=input_path,
                api_base_url=args.api.rstrip("/"),
                session_id=args.session_id,
                with_deepseek=args.with_deepseek,
                require_browser=args.require_browser,
            )
        )
    except (httpx.HTTPError, RuntimeError) as error:
        print(
            json.dumps(
                {"status": "failed", "error": str(error)},
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from error
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
