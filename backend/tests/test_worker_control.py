from __future__ import annotations

import asyncio

from app.settings import Settings
from app.worker import control


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, str]:
        return {
            "outcome": "stopped",
            "source_status": "stopped",
            "cleanup_status": "completed",
        }


class _Client:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.closed = False

    async def post(self, _url, *, json, headers):
        return _Response()

    async def aclose(self) -> None:
        self.closed = True


def test_owned_internal_clients_ignore_environment_proxies(monkeypatch) -> None:
    clients: list[_Client] = []

    def client_factory(**kwargs):
        client = _Client(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(control.httpx, "AsyncClient", client_factory)
    settings = Settings(
        internal_api_base_url="http://127.0.0.1:8000",
        internal_control_token="test-internal-control-token",
    )

    async def scenario() -> None:
        await control.publish_runtime_snapshot(
            settings=settings,
            session_id="session-1",
            snapshot={"room_connected": True},
        )
        await control.request_source_abort(
            settings=settings,
            session_id="session-1",
            reason="asr_stream_error",
            detail="provider failed",
            room_name="room-1",
            participant_identity="hls-session-1",
        )

    asyncio.run(scenario())

    assert len(clients) == 2
    assert all(client.kwargs["trust_env"] is False for client in clients)
    assert all(client.closed is True for client in clients)
