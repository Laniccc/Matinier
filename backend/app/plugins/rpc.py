from __future__ import annotations

import asyncio
import itertools
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from pydantic import BaseModel, ValidationError

from app.media.contracts import validate_bounded_json
from app.plugins.contracts import (
    ERROR_CAPABILITY_FAILED,
    ERROR_METHOD_NOT_FOUND,
    ERROR_PROTOCOL_INVALID,
    JsonRpcError,
    JsonRpcErrorResponse,
    JsonRpcRequest,
    JsonRpcResponse,
    RpcId,
)


RpcHandler = Callable[[dict[str, object]], Awaitable[object]]


class StreamWriterLike(Protocol):
    def write(self, data: bytes) -> None: ...
    async def drain(self) -> None: ...
    def close(self) -> None: ...
    async def wait_closed(self) -> None: ...


class JsonRpcProtocolError(RuntimeError):
    pass


class JsonRpcClosedError(ConnectionError):
    pass


class JsonRpcRemoteError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class RpcPublicError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass(frozen=True, slots=True)
class _HandlerSpec:
    handler: RpcHandler
    allow_notifications: bool
    write_effect: bool


class JsonRpcPeer:
    """A bounded, cancellation-aware, bidirectional JSON-RPC 2.0 peer."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: StreamWriterLike,
        *,
        max_line_bytes: int = 256 * 1024,
        max_message_bytes: int = 256 * 1024,
        max_pending_requests: int = 128,
    ) -> None:
        if max_line_bytes <= 0 or max_message_bytes <= 0:
            raise ValueError("JSON-RPC byte limits must be positive")
        if max_message_bytes > max_line_bytes:
            raise ValueError("decoded message limit cannot exceed line limit")
        if max_pending_requests <= 0:
            raise ValueError("max_pending_requests must be positive")
        self._reader = reader
        self._writer = writer
        self._max_line_bytes = max_line_bytes
        self._max_message_bytes = max_message_bytes
        self._max_pending_requests = max_pending_requests
        self._handlers: dict[str, _HandlerSpec] = {}
        self._pending: dict[RpcId, asyncio.Future[object]] = {}
        self._inbound: dict[RpcId, asyncio.Task[None]] = {}
        self._request_ids = itertools.count(1)
        self._write_lock = asyncio.Lock()
        self._read_task: asyncio.Task[None] | None = None
        self._read_error: Exception | None = None
        self._closing = False
        self._closed = False

    @property
    def pending_request_count(self) -> int:
        return len(self._pending)

    @property
    def closed(self) -> bool:
        return self._closed

    def register_handler(
        self,
        method: str,
        handler: RpcHandler,
        *,
        allow_notifications: bool = False,
        write_effect: bool = False,
    ) -> None:
        if method in self._handlers:
            raise ValueError(f"JSON-RPC handler is already registered: {method}")
        if allow_notifications and write_effect:
            raise ValueError("notifications cannot invoke write-effect handlers")
        JsonRpcRequest(id="handler-check", method=method, params={})
        self._handlers[method] = _HandlerSpec(
            handler=handler,
            allow_notifications=allow_notifications,
            write_effect=write_effect,
        )

    async def start(self) -> None:
        if self._read_task is not None and not self._read_task.done():
            return
        if self._closed:
            raise JsonRpcClosedError("JSON-RPC peer is closed")
        self._read_task = asyncio.create_task(
            self._read_loop(),
            name="plugin-json-rpc-reader",
        )

    async def request(
        self,
        method: str,
        params: BaseModel | dict[str, object],
        *,
        timeout: float,
    ) -> object:
        self._require_available()
        if timeout <= 0:
            raise ValueError("JSON-RPC timeout must be positive")
        if len(self._pending) >= self._max_pending_requests:
            raise JsonRpcProtocolError("JSON-RPC pending request limit exceeded")
        request_id = f"host-{next(self._request_ids)}"
        payload = params.model_dump(mode="json") if isinstance(params, BaseModel) else params
        envelope = JsonRpcRequest(id=request_id, method=method, params=payload)
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(envelope.model_dump(mode="json"))
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            except (TimeoutError, asyncio.CancelledError):
                if not future.done():
                    future.cancel()
                await self._send_cancel(request_id)
                raise
        finally:
            self._pending.pop(request_id, None)

    async def notify(
        self,
        method: str,
        params: BaseModel | dict[str, object],
    ) -> None:
        self._require_available()
        payload = params.model_dump(mode="json") if isinstance(params, BaseModel) else params
        validated = JsonRpcRequest(id="notification-check", method=method, params=payload)
        await self._send(
            {
                "jsonrpc": "2.0",
                "method": validated.method,
                "params": validated.params,
            }
        )

    async def wait_closed(self) -> None:
        task = self._read_task
        if task is not None:
            await asyncio.shield(task)
        if self._read_error is not None:
            raise self._read_error

    async def aclose(self) -> None:
        if self._closing:
            task = self._read_task
            if task is not None and task is not asyncio.current_task():
                await asyncio.gather(task, return_exceptions=True)
            return
        self._closing = True
        try:
            self._writer.close()
            await self._writer.wait_closed()
            task = self._read_task
            if task is not None and not task.done() and task is not asyncio.current_task():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._cancel_inbound()
            self._fail_pending(JsonRpcClosedError("JSON-RPC peer closed"))
        finally:
            self._closed = True

    async def _read_loop(self) -> None:
        try:
            while True:
                raw = await self._reader.readline()
                if not raw:
                    raise JsonRpcClosedError("JSON-RPC transport reached EOF")
                if len(raw) > self._max_line_bytes:
                    raise JsonRpcProtocolError("JSON-RPC line exceeds configured limit")
                if not raw.endswith(b"\n"):
                    raise JsonRpcProtocolError("JSON-RPC message is not newline-delimited")
                try:
                    decoded = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise JsonRpcProtocolError("JSON-RPC message is not valid JSON") from error
                try:
                    validate_bounded_json(
                        decoded,
                        max_bytes=self._max_message_bytes,
                    )
                except ValueError as error:
                    raise JsonRpcProtocolError(
                        "JSON-RPC decoded message exceeds configured limit"
                    ) from error
                if not isinstance(decoded, dict):
                    raise JsonRpcProtocolError("JSON-RPC envelope must be an object")
                await self._dispatch(decoded)
        except asyncio.CancelledError:
            if not self._closing:
                self._read_error = JsonRpcClosedError("JSON-RPC reader was cancelled")
        except Exception as error:
            self._read_error = error
        finally:
            if not self._closing:
                self._closed = True
                self._writer.close()
                self._cancel_inbound()
                self._fail_pending(
                    self._read_error or JsonRpcClosedError("JSON-RPC transport closed")
                )

    async def _dispatch(self, message: dict[str, Any]) -> None:
        if "method" in message:
            if "id" in message:
                await self._accept_request(message)
            else:
                await self._accept_notification(message)
            return
        if "result" in message or "error" in message:
            self._accept_response(message)
            return
        raise JsonRpcProtocolError("invalid JSON-RPC envelope")

    async def _accept_request(self, message: dict[str, Any]) -> None:
        try:
            request = JsonRpcRequest.model_validate(message)
        except ValidationError as error:
            raise JsonRpcProtocolError("invalid JSON-RPC request envelope") from error
        if request.id in self._inbound:
            await self._send_error(
                request.id,
                ERROR_PROTOCOL_INVALID,
                "duplicate request id",
            )
            return
        spec = self._handlers.get(request.method)
        if spec is None:
            await self._send_error(
                request.id,
                ERROR_METHOD_NOT_FOUND,
                "method not found",
            )
            return
        task = asyncio.create_task(
            self._run_handler(request, spec),
            name=f"plugin-json-rpc-handler-{request.method}",
        )
        self._inbound[request.id] = task
        task.add_done_callback(
            lambda completed, request_id=request.id: self._handler_done(
                request_id,
                completed,
            )
        )

    async def _accept_notification(self, message: dict[str, Any]) -> None:
        synthetic = dict(message)
        synthetic["id"] = "notification-check"
        try:
            notification = JsonRpcRequest.model_validate(synthetic)
        except ValidationError as error:
            raise JsonRpcProtocolError("invalid JSON-RPC notification envelope") from error
        if notification.method == "rpc.cancel":
            request_id = notification.params.get("id")
            if isinstance(request_id, str | int) and not isinstance(request_id, bool):
                task = self._inbound.get(request_id)
                if task is not None:
                    task.cancel()
            return
        spec = self._handlers.get(notification.method)
        if spec is None or not spec.allow_notifications:
            return
        task = asyncio.create_task(
            self._run_notification(notification.params, spec),
            name=f"plugin-json-rpc-notification-{notification.method}",
        )
        task.add_done_callback(self._consume_task_result)

    def _accept_response(self, message: dict[str, Any]) -> None:
        if "result" in message and "error" in message:
            raise JsonRpcProtocolError("JSON-RPC response cannot contain result and error")
        try:
            if "error" in message:
                response = JsonRpcErrorResponse.model_validate(message)
            else:
                response = JsonRpcResponse.model_validate(message)
        except ValidationError as error:
            raise JsonRpcProtocolError("invalid JSON-RPC response envelope") from error
        if response.id is None:
            return
        future = self._pending.get(response.id)
        if future is None or future.done():
            return
        if isinstance(response, JsonRpcErrorResponse):
            future.set_exception(
                JsonRpcRemoteError(
                    response.error.code,
                    response.error.message,
                    response.error.data,
                )
            )
        else:
            future.set_result(response.result)

    async def _run_handler(
        self,
        request: JsonRpcRequest,
        spec: _HandlerSpec,
    ) -> None:
        try:
            result = await spec.handler(request.params)
            response = JsonRpcResponse(id=request.id, result=result)
            await self._send(response.model_dump(mode="json"))
        except asyncio.CancelledError:
            raise
        except RpcPublicError as error:
            await self._send_error(
                request.id,
                error.code,
                error.message,
                data=error.data,
            )
        except Exception:
            await self._send_error(
                request.id,
                ERROR_CAPABILITY_FAILED,
                "handler failed",
            )

    @staticmethod
    async def _run_notification(
        params: dict[str, object],
        spec: _HandlerSpec,
    ) -> None:
        try:
            await spec.handler(params)
        except Exception:
            return

    async def _send_error(
        self,
        request_id: RpcId | None,
        code: str,
        message: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        envelope = JsonRpcErrorResponse(
            id=request_id,
            error=JsonRpcError(code=code, message=message, data=data),
        )
        await self._send(envelope.model_dump(mode="json"))

    async def _send_cancel(self, request_id: RpcId) -> None:
        if self._closed:
            return
        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "method": "rpc.cancel",
                    "params": {"id": request_id},
                }
            )
        except (JsonRpcClosedError, ConnectionError):
            return

    async def _send(self, message: dict[str, Any]) -> None:
        self._require_available()
        validate_bounded_json(message, max_bytes=self._max_message_bytes)
        encoded = json.dumps(
            message,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        if len(encoded) > self._max_line_bytes:
            raise JsonRpcProtocolError("JSON-RPC line exceeds configured limit")
        async with self._write_lock:
            self._writer.write(encoded)
            await self._writer.drain()

    def _require_available(self) -> None:
        if self._closed or self._closing:
            raise JsonRpcClosedError("JSON-RPC peer is closed")
        if self._read_task is None:
            raise JsonRpcClosedError("JSON-RPC peer has not been started")

    def _handler_done(
        self,
        request_id: RpcId,
        task: asyncio.Task[None],
    ) -> None:
        self._inbound.pop(request_id, None)
        self._consume_task_result(task)

    @staticmethod
    def _consume_task_result(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except asyncio.CancelledError:
            return

    def _cancel_inbound(self) -> None:
        for task in self._inbound.values():
            task.cancel()
        self._inbound.clear()

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

