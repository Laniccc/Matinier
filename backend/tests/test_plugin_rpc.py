from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.plugins.contracts import ERROR_CAPABILITY_FAILED, ERROR_PROTOCOL_INVALID
from app.plugins.rpc import (
    JsonRpcClosedError,
    JsonRpcPeer,
    JsonRpcProtocolError,
    JsonRpcRemoteError,
    RpcPublicError,
)


class MemoryWriter:
    def __init__(self, target: asyncio.StreamReader) -> None:
        self.target = target
        self.closed = False
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        if self.closed:
            raise ConnectionError("writer is closed")
        self.writes.append(data)
        self.target.feed_data(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.target.feed_eof()

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)


def peer_pair(**kwargs: Any) -> tuple[JsonRpcPeer, JsonRpcPeer]:
    left_reader = asyncio.StreamReader(limit=2_000_000)
    right_reader = asyncio.StreamReader(limit=2_000_000)
    left = JsonRpcPeer(left_reader, MemoryWriter(right_reader), **kwargs)
    right = JsonRpcPeer(right_reader, MemoryWriter(left_reader), **kwargs)
    return left, right


def test_request_response_correlation_and_bidirectional_dispatch() -> None:
    async def scenario() -> None:
        left, right = peer_pair()
        release = asyncio.Event()
        started = asyncio.Event()

        async def slow(params: dict[str, object]) -> dict[str, object]:
            started.set()
            await release.wait()
            return {"slow": params["value"]}

        async def echo(params: dict[str, object]) -> dict[str, object]:
            return {"echo": params["value"]}

        right.register_handler("test.slow", slow)
        left.register_handler("test.echo", echo)
        await left.start()
        await right.start()

        pending = asyncio.create_task(
            left.request("test.slow", {"value": 1}, timeout=1)
        )
        await started.wait()
        reverse = await right.request("test.echo", {"value": 2}, timeout=1)
        assert reverse == {"echo": 2}
        release.set()
        assert await pending == {"slow": 1}
        await left.aclose()
        await right.aclose()

    asyncio.run(scenario())


def test_bounded_lines_decoded_messages_and_malformed_json_fail_closed() -> None:
    async def oversized_line() -> None:
        incoming = asyncio.StreamReader(limit=2_000_000)
        responses = asyncio.StreamReader(limit=2_000_000)
        peer = JsonRpcPeer(
            incoming,
            MemoryWriter(responses),
            max_line_bytes=128,
            max_message_bytes=128,
        )
        await peer.start()
        incoming.feed_data(b"{" + b"x" * 200 + b"}\n")
        with pytest.raises(JsonRpcProtocolError, match="line"):
            await peer.wait_closed()

    async def malformed_json() -> None:
        incoming = asyncio.StreamReader(limit=2_000_000)
        peer = JsonRpcPeer(incoming, MemoryWriter(asyncio.StreamReader()))
        await peer.start()
        incoming.feed_data(b"{not-json}\n")
        with pytest.raises(JsonRpcProtocolError, match="JSON"):
            await peer.wait_closed()

    asyncio.run(oversized_line())
    asyncio.run(malformed_json())


def test_duplicate_inbound_request_id_gets_stable_protocol_error() -> None:
    async def scenario() -> None:
        incoming = asyncio.StreamReader(limit=2_000_000)
        responses = asyncio.StreamReader(limit=2_000_000)
        peer = JsonRpcPeer(incoming, MemoryWriter(responses))
        release = asyncio.Event()

        async def slow(_params: dict[str, object]) -> dict[str, bool]:
            await release.wait()
            return {"ok": True}

        peer.register_handler("test.slow", slow)
        await peer.start()
        message = json.dumps(
            {"jsonrpc": "2.0", "id": "duplicate", "method": "test.slow", "params": {}}
        ).encode() + b"\n"
        incoming.feed_data(message + message)
        error = json.loads(await responses.readline())
        assert error["error"]["code"] == ERROR_PROTOCOL_INVALID
        release.set()
        success = json.loads(await responses.readline())
        assert success["result"] == {"ok": True}
        await peer.aclose()

    asyncio.run(scenario())


def test_deadline_and_caller_cancellation_cancel_remote_handler() -> None:
    async def scenario() -> None:
        left, right = peer_pair()
        cancelled = asyncio.Event()

        async def blocks(_params: dict[str, object]) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        right.register_handler("test.blocks", blocks)
        await left.start()
        await right.start()
        with pytest.raises(TimeoutError):
            await left.request("test.blocks", {}, timeout=0.02)
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        assert left.pending_request_count == 0
        await left.aclose()
        await right.aclose()

    asyncio.run(scenario())


def test_eof_fails_pending_requests_and_graceful_close_is_idempotent() -> None:
    async def scenario() -> None:
        incoming = asyncio.StreamReader(limit=2_000_000)
        remote_input = asyncio.StreamReader(limit=2_000_000)
        peer = JsonRpcPeer(incoming, MemoryWriter(remote_input))
        await peer.start()
        pending = asyncio.create_task(peer.request("test.never", {}, timeout=5))
        await asyncio.sleep(0)
        incoming.feed_eof()
        with pytest.raises(JsonRpcClosedError, match="EOF"):
            await pending
        await peer.aclose()
        await peer.aclose()
        assert peer.closed is True

    asyncio.run(scenario())


def test_handler_errors_are_sanitized_but_public_errors_are_preserved() -> None:
    async def scenario() -> None:
        left, right = peer_pair()

        async def secret_failure(_params: dict[str, object]) -> None:
            raise RuntimeError("database password=do-not-leak")

        async def public_failure(_params: dict[str, object]) -> None:
            raise RpcPublicError(
                ERROR_PROTOCOL_INVALID,
                "invalid request",
                data={"field": "scope"},
            )

        right.register_handler("test.secret", secret_failure)
        right.register_handler("test.public", public_failure)
        await left.start()
        await right.start()

        with pytest.raises(JsonRpcRemoteError) as secret:
            await left.request("test.secret", {}, timeout=1)
        assert secret.value.code == ERROR_CAPABILITY_FAILED
        assert "password" not in secret.value.message

        with pytest.raises(JsonRpcRemoteError) as public:
            await left.request("test.public", {}, timeout=1)
        assert public.value.code == ERROR_PROTOCOL_INVALID
        assert public.value.data == {"field": "scope"}
        await left.aclose()
        await right.aclose()

    asyncio.run(scenario())


def test_stderr_is_not_part_of_protocol_framing_and_notifications_are_read_only() -> None:
    async def scenario() -> None:
        left, right = peer_pair()
        log_stream = asyncio.StreamReader()
        log_stream.feed_data(b"plugin log: {not json}\n")
        observed = asyncio.Event()

        async def notification(_params: dict[str, object]) -> None:
            observed.set()

        right.register_handler(
            "audit.observe",
            notification,
            allow_notifications=True,
        )
        await left.start()
        await right.start()
        await left.notify("audit.observe", {"value": 1})
        await asyncio.wait_for(observed.wait(), timeout=1)
        assert await log_stream.readline() == b"plugin log: {not json}\n"

        with pytest.raises(ValueError, match="notification"):
            right.register_handler(
                "state.write",
                notification,
                allow_notifications=True,
                write_effect=True,
            )
        await left.aclose()
        await right.aclose()

    asyncio.run(scenario())
