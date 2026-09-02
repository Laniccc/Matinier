from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SDK_PYTHON = ROOT / "plugin-sdk" / "python"
if str(SDK_PYTHON) not in sys.path:
    sys.path.insert(0, str(SDK_PYTHON))

from matinier_plugin import (  # noqa: E402
    PluginError,
    PluginRemoteError,
    PluginRuntime,
    PluginRuntimeClosedError,
)


class MemoryReader:
    def __init__(self) -> None:
        self._lines: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self) -> bytes:
        return await self._lines.get()

    def feed(self, message: dict[str, object]) -> None:
        self._lines.put_nowait(
            json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        )

    def close(self) -> None:
        self._lines.put_nowait(b"")


class MemoryWriter:
    def __init__(self) -> None:
        self.lines: list[bytes] = []
        self.changed = asyncio.Event()
        self.active_drains = 0
        self.max_active_drains = 0

    def write(self, data: bytes) -> None:
        self.lines.append(data)

    async def drain(self) -> None:
        self.active_drains += 1
        self.max_active_drains = max(self.max_active_drains, self.active_drains)
        await asyncio.sleep(0)
        self.active_drains -= 1
        self.changed.set()


async def output_at(writer: MemoryWriter, index: int) -> dict[str, object]:
    async def wait() -> dict[str, object]:
        while len(writer.lines) <= index:
            writer.changed.clear()
            await writer.changed.wait()
        line = writer.lines[index]
        assert line.endswith(b"\n")
        assert line.count(b"\n") == 1
        return json.loads(line)

    return await asyncio.wait_for(wait(), timeout=1)


def request(request_id: int, method: str, params: dict[str, object] | None = None):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params or {},
    }


def test_host_requests_are_concurrent_and_errors_are_public() -> None:
    async def scenario() -> None:
        reader = MemoryReader()
        writer = MemoryWriter()
        runtime = PluginRuntime(reader=reader, writer=writer)
        release = asyncio.Event()

        async def slow(_params: dict[str, object]) -> dict[str, object]:
            await release.wait()
            return {"done": True}

        async def heartbeat(_params: dict[str, object]) -> dict[str, object]:
            return {"ok": True}

        async def public_failure(_params: dict[str, object]) -> object:
            raise PluginError("plugin.scope.invalid", "Session scope is stale")

        async def private_failure(_params: dict[str, object]) -> object:
            raise RuntimeError("secret provider response")

        runtime.register("job.slow", slow)
        runtime.register("plugin.heartbeat", heartbeat)
        runtime.register("test.public_failure", public_failure)
        runtime.register("test.private_failure", private_failure)
        run_task = asyncio.create_task(runtime.run())

        reader.feed(request(1, "job.slow"))
        reader.feed(request(2, "plugin.heartbeat"))
        assert await output_at(writer, 0) == {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"ok": True},
        }
        release.set()
        assert (await output_at(writer, 1))["id"] == 1

        reader.feed(request(3, "test.public_failure"))
        public = await output_at(writer, 2)
        assert public["error"] == {
            "code": "plugin.scope.invalid",
            "message": "Session scope is stale",
            "data": None,
        }
        reader.feed(request(4, "test.private_failure"))
        private = await output_at(writer, 3)
        assert private["error"]["code"] == "plugin.capability.failed"
        assert "secret provider response" not in json.dumps(private)

        reader.close()
        await run_task

    asyncio.run(scenario())


def test_capability_ids_correlate_out_of_order_and_handler_does_not_deadlock() -> None:
    async def scenario() -> None:
        reader = MemoryReader()
        writer = MemoryWriter()
        runtime = PluginRuntime(reader=reader, writer=writer)

        async def event_batch(params: dict[str, object]) -> dict[str, object]:
            saved = await runtime.capability(
                name="state.put",
                session_scope=str(params["session_scope"]),
                input_value={
                    "key": "cursor",
                    "value": {"sequence": 8},
                    "expected_version": 0,
                },
            )
            assert saved == {"version": 1}
            return {"acknowledged_sequence": 8}

        runtime.register("event.batch", event_batch)
        run_task = asyncio.create_task(runtime.run())
        reader.feed(request(1, "event.batch", {"session_scope": "scope-1"}))
        outbound = await output_at(writer, 0)
        assert outbound["method"] == "capability.invoke"
        assert outbound["params"]["capability"] == "state.put"
        reader.feed(
            {"jsonrpc": "2.0", "id": outbound["id"], "result": {"version": 1}}
        )
        assert await output_at(writer, 1) == {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"acknowledged_sequence": 8},
        }

        first = asyncio.create_task(
            runtime.capability(
                name="state.get",
                session_scope="scope-1",
                input_value={"key": "first"},
            )
        )
        second = asyncio.create_task(
            runtime.capability(
                name="state.get",
                session_scope="scope-1",
                input_value={"key": "second"},
            )
        )
        first_request = await output_at(writer, 2)
        second_request = await output_at(writer, 3)
        assert first_request["id"] != second_request["id"]
        reader.feed(
            {"jsonrpc": "2.0", "id": second_request["id"], "result": {"value": 2}}
        )
        reader.feed(
            {"jsonrpc": "2.0", "id": first_request["id"], "result": {"value": 1}}
        )
        assert await first == {"value": 1}
        assert await second == {"value": 2}

        reader.close()
        await run_task

    asyncio.run(scenario())


def test_background_capability_survives_command_response_and_remote_error_maps() -> None:
    async def scenario() -> None:
        reader = MemoryReader()
        writer = MemoryWriter()
        runtime = PluginRuntime(reader=reader, writer=writer)
        begin_background = asyncio.Event()
        finished = asyncio.Event()
        observed: list[str] = []

        async def background() -> None:
            await begin_background.wait()
            try:
                await runtime.capability(
                    name="model.invoke",
                    session_scope="scope-1",
                    input_value={"task": "notes"},
                )
            except PluginRemoteError as error:
                observed.append(error.code)
            finally:
                finished.set()

        async def command(_params: dict[str, object]) -> dict[str, object]:
            runtime.create_task(background(), name="background-notes")
            return {"accepted": True}

        runtime.register("command.execute", command)
        run_task = asyncio.create_task(runtime.run())
        reader.feed(request(1, "command.execute"))
        assert (await output_at(writer, 0))["result"] == {"accepted": True}
        begin_background.set()
        outbound = await output_at(writer, 1)
        reader.feed(
            {
                "jsonrpc": "2.0",
                "id": outbound["id"],
                "error": {
                    "code": "plugin.capability.failed",
                    "message": "Model unavailable",
                    "data": None,
                },
            }
        )
        await asyncio.wait_for(finished.wait(), timeout=1)
        assert observed == ["plugin.capability.failed"]

        reader.close()
        await run_task

    asyncio.run(scenario())


def test_writer_is_serialized_and_shutdown_cancels_owned_and_pending_tasks() -> None:
    async def scenario() -> None:
        reader = MemoryReader()
        writer = MemoryWriter()
        errors = io.StringIO()
        runtime = PluginRuntime(reader=reader, writer=writer, error_stream=errors)
        cancelled = asyncio.Event()

        async def forever() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        runtime.create_task(forever(), name="forever")
        run_task = asyncio.create_task(runtime.run())
        calls = [
            asyncio.create_task(
                runtime.capability(
                    name="state.get",
                    session_scope="scope-1",
                    input_value={"key": f"key-{index}"},
                )
            )
            for index in range(20)
        ]
        for index in range(20):
            await output_at(writer, index)
        assert writer.max_active_drains == 1
        assert len({json.loads(line)["id"] for line in writer.lines}) == 20

        runtime.log("diagnostic only")
        assert errors.getvalue() == "diagnostic only\n"
        assert all(b"diagnostic only" not in line for line in writer.lines)

        reader.close()
        await run_task
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        results = await asyncio.gather(*calls, return_exceptions=True)
        assert all(isinstance(item, PluginRuntimeClosedError) for item in results)

    asyncio.run(scenario())
