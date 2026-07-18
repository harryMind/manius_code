import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from manius_code.core.bus.envelope import JsonRpcError, JsonRpcRequest, JsonRpcSuccess

logger = logging.getLogger(__name__)
EventHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


class IpcError(RuntimeError):
    # 保存 JSON-RPC 错误码和错误消息。
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class SocketClient:
    # 保存 daemon 地址和长连接状态。
    def __init__(self, host: str, port: int, event_handler: EventHandler | None = None) -> None:
        self._host = host
        self._port = port
        self._event_handler = event_handler
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._event_loop_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[JsonRpcSuccess]] = {}

    # 建立连接并启动响应与事件读取循环。
    async def connect(self) -> None:
        if self._writer is not None:
            raise RuntimeError("SocketClient is already connected")
        self._reader, self._writer = await asyncio.open_connection(self._host, self._port)
        self._event_loop_task = asyncio.create_task(self.run_event_loop())

    # 关闭连接、停止读取循环并通知所有等待中的请求。
    async def close(self) -> None:
        event_loop_task = self._event_loop_task
        self._event_loop_task = None
        if event_loop_task is not None and event_loop_task is not asyncio.current_task():
            event_loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await event_loop_task
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        self._fail_pending(IpcError(-32000, "SocketClient connection closed"))

    # 发送命令并等待由 UUID 请求 ID 匹配的 JSON-RPC 响应。
    async def send_command(self, method: str, params: dict[str, Any]) -> JsonRpcSuccess:
        if self._writer is None:
            raise RuntimeError("SocketClient is not connected")
        request_id = str(uuid4())
        future: asyncio.Future[JsonRpcSuccess] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            request = JsonRpcRequest(jsonrpc="2.0", id=request_id, method=method, params=params)
            self._writer.write(request.model_dump_json().encode() + b"\n")
            await self._writer.drain()
            return await future
        finally:
            self._pending.pop(request_id, None)

    # 持续读取长连接消息并分发响应或服务端事件。
    async def run_event_loop(self) -> None:
        if self._reader is None:
            raise RuntimeError("SocketClient is not connected")
        try:
            while line := await self._reader.readline():
                try:
                    await self.message_dispatch(line)
                except IpcError as error:
                    logger.warning("Invalid IPC message: %s", error)
                    self._fail_pending(error)
        finally:
            self._fail_pending(IpcError(-32000, "SocketClient connection closed"))

    # 根据消息 ID 唤醒等待请求，或将服务端推送交给事件接口。
    async def message_dispatch(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            raise IpcError(-32700, "Received invalid JSON") from error
        if not isinstance(message, dict):
            raise IpcError(-32600, "Received invalid JSON-RPC message")
        request_id = message.get("id")
        if isinstance(request_id, str) and request_id in self._pending:
            future = self._pending[request_id]
            try:
                response = JsonRpcSuccess.model_validate(message)
            except ValidationError:
                try:
                    error = JsonRpcError.model_validate(message).error
                except ValidationError as validation_error:
                    future.set_exception(IpcError(-32600, "Received invalid JSON-RPC response"))
                    raise IpcError(-32600, "Received invalid JSON-RPC response") from validation_error
                future.set_exception(IpcError(error.code, error.message))
            else:
                future.set_result(response)
            return
        if "method" in message:
            await self.on_event(message)
            return
        logger.warning("Ignoring JSON-RPC message with unknown request ID: %r", request_id)

    # 为未来服务端事件推送预留处理接口。
    async def on_event(self, event: dict[str, Any]) -> None:
        if self._event_handler is None:
            return
        result = self._event_handler(event)
        if inspect.isawaitable(result):
            await result

    # 以同一异常结束全部尚未匹配响应的请求。
    def _fail_pending(self, error: IpcError) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
