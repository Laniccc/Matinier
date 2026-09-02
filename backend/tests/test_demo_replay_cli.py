from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_demo_replay import resolve_session


EXISTING_SESSION_ID = "11111111-1111-4111-8111-111111111111"


def _session(session_id: str, *, source_type: str = "file") -> dict[str, object]:
    return {
        "id": session_id,
        "room_name": f"live-caption-{session_id[:12]}",
        "status": "created",
        "source_type": source_type,
        "source_name": "demo_audio.wav",
        "language": "zh-CN",
        "started_at": None,
        "ended_at": None,
        "created_at": "2026-07-22T00:00:00Z",
    }


def test_resolve_session_reuses_existing_session_without_posting() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json=_session(EXISTING_SESSION_ID))

    async def scenario() -> dict[str, object]:
        async with httpx.AsyncClient(
            base_url="http://api.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            return await resolve_session(
                client,
                input_path=Path("demo_audio.wav"),
                language="zh-CN",
                session_id=EXISTING_SESSION_ID,
            )

    session = asyncio.run(scenario())

    assert session["id"] == EXISTING_SESSION_ID
    assert requests == [("GET", f"/api/sessions/{EXISTING_SESSION_ID}")]


def test_resolve_session_preserves_create_new_file_session_default() -> None:
    requests: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            (
                request.method,
                request.url.path,
                json.loads(request.content.decode("utf-8")),
            )
        )
        return httpx.Response(201, json=_session(EXISTING_SESSION_ID))

    async def scenario() -> dict[str, object]:
        async with httpx.AsyncClient(
            base_url="http://api.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            return await resolve_session(
                client,
                input_path=Path("demo_audio.wav"),
                language="en-US",
                session_id=None,
            )

    session = asyncio.run(scenario())

    assert session["id"] == EXISTING_SESSION_ID
    assert requests == [
        (
            "POST",
            "/api/sessions",
            {
                "source_type": "file",
                "source_name": "demo_audio.wav",
                "language": "en-US",
            },
        )
    ]

