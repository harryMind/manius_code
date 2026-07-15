import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from manius_code.core.bus.envelope import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcSuccess,
    make_error,
)

CommandHandler = Callable[[dict[str, Any]], Awaitable[Any]]
logger = logging.getLogger(__name__)


class SocketServer:
    # 保存监听地址、端口和已注册的命令处理器。
    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._server: asyncio.AbstractServer | None = None
        self._handlers: dict[str, CommandHandler] = {}

    # 为指定 JSON-RPC 方法注册异步命令处理器。
    def register(self, method: str, handler: CommandHandler) -> None:
        self._handlers[method] = handler

    # 确认目标地址没有被已有 daemon 占用。
    async def _ensure_port_is_available(self) -> None:
        try:
            _, writer = await asyncio.open_connection(self._host, self._port)
        except OSError:
            return
        writer.close()
        await writer.wait_closed()
        raise RuntimeError(f"Another manius-core instance is listening on {self._host}:{self._port}")

    # 启动 TCP 服务器并开始接受 NDJSON 客户端连接。
    async def start(self) -> None:
        await self._ensure_port_is_available()
        self._server = await asyncio.start_server(self._handle_client, self._host, self._port,limit=64 * 1024 * 1024)
        logger.info("manius-core listening on %s:%s", self._host, self._port)

    # 停止接受连接并等待现有服务器关闭。
    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    # 管理单个客户端连接的读取任务和关闭流程。
    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._read_loop(reader, writer)
        finally:
            writer.close()
            await writer.wait_closed()

    # 为每一行请求独立调度分发任务，避免长处理器阻塞读取。
    async def _read_loop(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        tasks: set[asyncio.Task[None]] = set()
        while line := await reader.readline():
            task = asyncio.create_task(self._dispatch(line, writer))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # 处理一条请求并将 JSON-RPC 响应发送给客户端。
    async def _dispatch(self, line: bytes, writer: asyncio.StreamWriter) -> None:
        try:
            response = await self._make_response(line)
            writer.write(response.model_dump_json().encode() + b"\n")
            await writer.drain()
        except (ConnectionError, OSError):
            logger.debug("Client disconnected before receiving a response")

    # 验证请求、调用已注册处理器并封装 JSON-RPC 响应。
    async def _make_response(self, line: bytes) -> JsonRpcSuccess | JsonRpcError:
        try:
            raw_request = json.loads(line)
        except json.JSONDecodeError:
            return make_error(None, PARSE_ERROR, "Parse error")
        try:
            request = JsonRpcRequest.model_validate(raw_request)
        except ValidationError:
            request_id = raw_request.get("id") if isinstance(raw_request, dict) else None
            return make_error(request_id, INVALID_REQUEST, "Invalid Request")
        handler = self._handlers.get(request.method)
        if handler is None:
            return make_error(request.id, METHOD_NOT_FOUND, "Method not found")
        try:
            result = await handler(request.params)
        except ValidationError:
            return make_error(request.id, INVALID_PARAMS, "Invalid params")
        except Exception:
            logger.exception("Unhandled command error for %s", request.method)
            return make_error(request.id, INTERNAL_ERROR, "Internal error")
        return JsonRpcSuccess(id=request.id, result=self._serialize_result(result))

    # 将 Pydantic 返回对象转换为 JSON-RPC 可序列化结果。
    def _serialize_result(self, result: Any) -> Any:
        return result.model_dump() if isinstance(result, BaseModel) else result
