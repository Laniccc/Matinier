from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app.settings import Settings


logger = logging.getLogger(__name__)


class SourceAbortRequestError(RuntimeError):
    """Raised when the API cannot acknowledge an internal source abort."""


@dataclass(frozen=True)
class SourceAbortResult:
    request_id: str
    outcome: str
    source_status: str
    cleanup_status: str


async def publish_runtime_snapshot(
    *,
    settings: Settings,
    session_id: str,
    snapshot: dict[str, Any],
    client: httpx.AsyncClient | None = None,
) -> None:
    url = (
        str(settings.internal_api_base_url).rstrip("/")
        + f"/internal/sessions/{session_id}/runtime"
    )
    headers = {
        "X-Internal-Control-Token": settings.internal_control_token,
    }
    owns_client = client is None
    http_client = client or httpx.AsyncClient(
        timeout=settings.internal_control_timeout_seconds,
        # Loopback control traffic must never inherit a workstation proxy.
        # Besides leaking an internal bearer token to the proxy path, doing so
        # can delay ASR startup and prevent source-abort cleanup entirely.
        trust_env=False,
    )
    try:
        response = await http_client.post(url, json=snapshot, headers=headers)
        response.raise_for_status()
    finally:
        if owns_client:
            await http_client.aclose()


async def request_source_abort(
    *,
    settings: Settings,
    session_id: str,
    reason: str,
    detail: str,
    room_name: str,
    participant_identity: str,
    client: httpx.AsyncClient | None = None,
) -> SourceAbortResult:
    request_id = str(uuid.uuid4())
    url = (
        str(settings.internal_api_base_url).rstrip("/")
        + f"/internal/sessions/{session_id}/source/abort"
    )
    payload = {
        "reason": reason,
        "detail": detail[:1000],
        "requested_by": "caption_worker",
        "request_id": request_id,
    }
    headers = {
        "X-Internal-Control-Token": settings.internal_control_token,
    }
    owns_client = client is None
    http_client = client or httpx.AsyncClient(
        timeout=settings.internal_control_timeout_seconds,
        trust_env=False,
    )
    logger.warning(
        "source abort requested",
        extra={
            "process_name": "worker",
            "session_id": session_id,
            "room_name": room_name,
            "participant_identity": participant_identity,
            "event": "source_abort_requested",
            "source_type": "hls",
            "status": "failed",
            "failure_code": reason,
            "cleanup_status": "in_progress",
            "request_id": request_id,
        },
    )
    try:
        response = await http_client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        result = SourceAbortResult(
            request_id=request_id,
            outcome=str(body["outcome"]),
            source_status=str(body["source_status"]),
            cleanup_status=str(body["cleanup_status"]),
        )
        logger.info(
            "source abort acknowledged",
            extra={
                "process_name": "worker",
                "session_id": session_id,
                "room_name": room_name,
                "participant_identity": participant_identity,
                "event": "source_abort_acknowledged",
                "failure_code": reason,
                "cleanup_status": result.cleanup_status,
                "request_id": request_id,
            },
        )
        return result
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
        raise SourceAbortRequestError(
            f"Internal source abort was not acknowledged: {type(error).__name__}"
        ) from error
    finally:
        if owns_client:
            await http_client.aclose()
