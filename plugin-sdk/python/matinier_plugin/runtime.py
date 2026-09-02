from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import re
import sys
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TextIO

from .contracts import (
    ERROR_CAPABILITY_FAILED,
    ERROR_METHOD_NOT_FOUND,
    ERROR_PROTOCOL_INVALID,
    MAX_RPC_MESSAGE_BYTES,
    PUBLIC_ERROR_CODES,
    PluginError,
    PluginRemoteError,
    PluginRuntimeClosedError,
    validate_json_value,
    validate_rpc_id,
)


AsyncHandler = Callable[[dict[str, object]], Awaitable[object]]
_METHOD = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


class AsyncLineReader(Protocol):
    async def readline(self) -> bytes: ...


class AsyncLineWriter(Protocol):
    def write(self, data: bytes) -> object: ...
    async def drain(self) -> None: ...


class _StandardInputReader:
    async def readline(self) -> bytes:
        return await asyncio.to_thread(sys.stdin.buffer.readline)


class _StandardOutputWriter:
    def write(self, data: bytes) -> object:
        return sys.stdout.buffer.write(data)

    async def drain(self) -> None:
        await asyncio.to_thread(sys.stdout.buffer.flush)


class PluginRuntime:
    """Dependency-free concurrent JSON-RPC runtime for an isolated plugin."""

    def __init__(
        self,
        *,
        reader: AsyncLineReader | None = None,
        writer: AsyncLineWriter | None = None,
        error_stream: TextIO | None = None,
        max_message_bytes: int = MAX_RPC_MESSAGE_BYTES,
    ) -> None:
        if max_message_bytes < 1 or max_message_bytes > MAX_RPC_MESSAGE_BYTES:
            raise ValueError("invalid plugin RPC message limit")
        self._reader = reader or _StandardInputReader()
        self._writer = writer or _StandardOutputWriter()
        self._error_stream = error_stream or sys.stderr
        self._max_message_bytes = max_message_bytes
        self._handlers: dict[str, AsyncHandler] = {}
        self._request_ids = itertools.count(1)
        self._pending: dict[str | int, asyncio.Future[dict[str, object]]] = {}
        self._owned_tasks: set[asyncio.Task[object]] = set()
        self._write_lock = asyncio.Lock()
        self._running = False
        self._closed = False

    def register(self, method: str, handler: AsyncHandler) -> None:
        if _METHOD.fullmatch(method) is None:
            raise ValueError("invalid plugin method name")
        if method in self._handlers:
            raise ValueError(f"plugin method is already registered: {method}")
        self._handlers[method] = handler

    async def capability(
        self,
        *,
        name: str,
        session_scope: str,
        input_value: dict[str, object],
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        if self._closed:
            raise PluginRuntimeClosedError("plugin runtime is closed")
        if _METHOD.fullmatch(name) is None:
            raise ValueError("invalid capability name")
        if not session_scope:
            raise ValueError("session scope is required")
        if idempotency_key is not None and not idempotency_key:
            raise ValueError("idempotency key must not be empty")
        validate_json_value(input_value)
        request_id = f"plugin-{next(self._request_ids)}"
        loop = asyncio.get_running_loop()
        response: asyncio.Future[dict[str, object]] = loop.create_future()
        self._pending[request_id] = response
        params: dict[str, object] = {
            "capability": name,
            "session_scope": session_scope,
            "input": input_value,
        }
        if idempotency_key is not None:
            params["idempotency_key"] = idempotency_key
        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "capability.invoke",
                    "params": params,
                }
            )
            return await response
        finally:
            self._pending.pop(request_id, None)

    def create_task(
        self,
        awaitable: Awaitable[Any],
        *,
        name: str,
    ) -> asyncio.Task[Any]:
        if self._closed:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise PluginRuntimeClosedError("plugin runtime is closed")
        task = asyncio.create_task(awaitable, name=name)
        self._owned_tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("plugin runtime is already running")
        if self._closed:
            raise PluginRuntimeClosedError("plugin runtime is closed")
        self._running = True
        try:
            while not self._closed:
                raw_line = await self._reader.readline()
                if not raw_line:
                    break
                if len(raw_line) > self._max_message_bytes + 1:
                    await self._send_error(
                        None,
                        ERROR_PROTOCOL_INVALID,
                        "JSON-RPC message exceeds the byte limit",
                    )
                    continue
                try:
                    message = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    await self._send_error(
                        None,
                        ERROR_PROTOCOL_INVALID,
                        "Invalid JSON-RPC message",
                    )
                    continue
                try:
                    await self._accept(message)
                except ValueError:
                    await self._send_error(
                        None,
                        ERROR_PROTOCOL_INVALID,
                        "Invalid JSON-RPC envelope",
                    )
        finally:
            self._running = False
            await self.aclose()

    async def aclose(self) -> None:
        if self._closed and not self._owned_tasks and not self._pending:
            return
        self._closed = True
        close_error = PluginRuntimeClosedError("plugin runtime transport closed")
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(close_error)
        current = asyncio.current_task()
        tasks = [task for task in self._owned_tasks if task is not current]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._owned_tasks.clear()

    def log(self, message: str) -> None:
        print(str(message), file=self._error_stream, flush=True)

    async def _accept(self, raw_message: object) -> None:
        if not isinstance(raw_message, dict) or raw_message.get("jsonrpc") != "2.0":
            raise ValueError("invalid JSON-RPC envelope")
        message = dict(raw_message)
        if "method" in message:
            if set(message) - {"jsonrpc", "id", "method", "params"}:
                raise ValueError("request contains unknown fields")
            method = message.get("method")
            params = message.get("params", {})
            if not isinstance(method, str) or _METHOD.fullmatch(method) is None:
                raise ValueError("invalid method")
            if not isinstance(params, dict):
                raise ValueError("invalid params")
            validate_json_value(params)
            request_id: str | int | None = None
            if "id" in message:
                request_id = validate_rpc_id(message["id"])
            self.create_task(
                self._dispatch(method, params, request_id),
                name=f"host-request-{method}",
            )
            return
        await self._accept_response(message)

    async def _accept_response(self, message: dict[str, object]) -> None:
        if set(message) - {"jsonrpc", "id", "result", "error"}:
            raise ValueError("response contains unknown fields")
        if "id" not in message or ("result" in message) == ("error" in message):
            raise ValueError("invalid response")
        request_id = validate_rpc_id(message["id"])
        future = self._pending.get(request_id)
        if future is None or future.done():
            return
        if "error" in message:
            raw_error = message["error"]
            if not isinstance(raw_error, dict):
                raise ValueError("invalid error response")
            if set(raw_error) - {"code", "message", "data"}:
                raise ValueError("error response contains unknown fields")
            code = raw_error.get("code")
            error_message = raw_error.get("message")
            data = raw_error.get("data")
            if code not in PUBLIC_ERROR_CODES or not isinstance(error_message, str):
                raise ValueError("invalid public error")
            if data is not None and not isinstance(data, dict):
                raise ValueError("invalid error data")
            future.set_exception(PluginRemoteError(code, error_message, data))
            return
        result = message["result"]
        if not isinstance(result, dict):
            future.set_exception(
                PluginRemoteError(
                    ERROR_PROTOCOL_INVALID,
                    "Host capability returned a non-object result",
                )
            )
            return
        validate_json_value(result)
        future.set_result(result)

    async def _dispatch(
        self,
        method: str,
        params: dict[str, object],
        request_id: str | int | None,
    ) -> None:
        handler = self._handlers.get(method)
        if handler is None:
            if request_id is not None:
                await self._send_error(
                    request_id,
                    ERROR_METHOD_NOT_FOUND,
                    "Method is not supported",
                )
            return
        try:
            result = await handler(params)
            if request_id is not None:
                await self._send(
                    {"jsonrpc": "2.0", "id": request_id, "result": result}
                )
        except asyncio.CancelledError:
            raise
        except PluginError as error:
            if request_id is not None:
                await self._send_error(
                    request_id,
                    error.code,
                    error.message,
                    data=error.data,
                )
        except Exception:
            if request_id is not None:
                await self._send_error(
                    request_id,
                    ERROR_CAPABILITY_FAILED,
                    "Handler failed",
                )

    async def _send_error(
        self,
        request_id: str | int | None,
        code: str,
        message: str,
        *,
        data: dict[str, object] | None = None,
    ) -> None:
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message, "data": data},
            }
        )

    async def _send(self, message: dict[str, object]) -> None:
        if self._closed:
            raise PluginRuntimeClosedError("plugin runtime is closed")
        encoded = json.dumps(
            message,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > self._max_message_bytes:
            raise ValueError("JSON-RPC message exceeds the byte limit")
        async with self._write_lock:
            wrote = self._writer.write(encoded + b"\n")
            if inspect.isawaitable(wrote):
                await wrote
            await self._writer.drain()

    def _task_done(self, task: asyncio.Task[object]) -> None:
        self._owned_tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            self.log(f"background task failed: {type(error).__name__}")


__all__ = ["AsyncHandler", "PluginRuntime"]
