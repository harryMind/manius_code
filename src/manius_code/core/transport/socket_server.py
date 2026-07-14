import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pydantic import ValidationError

from ..bus.commands import PingCommand, PongResult
from ..bus.envelope import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    JsonRpcRequest,
    JsonRpcError,
    JsonRpcSuccess,
    make_error,
)

CommandHandler = Callable[[PingCommand], Awaitable[PongResult]]
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
        self._server = await asyncio.start_server(self._handle_client, self._host, self._port)
        logger.info("manius-core listening on %s:%s", self._host, self._port)

    # 停止接受连接并等待现有服务器关闭。
    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    # 读取客户端 NDJSON 请求并依次返回 JSON-RPC 响应。
    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                response = await self._dispatch(line)
                writer.write(response.model_dump_json().encode() + b"\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    # 验证请求、调用对应处理器并转换为协议响应。
    async def _dispatch(self, line: bytes) -> JsonRpcSuccess | JsonRpcError:
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
            command = PingCommand.model_validate(request.params)
        except ValidationError:
            return make_error(request.id, INVALID_PARAMS, "Invalid params")
        if command.type != request.method:
            return make_error(request.id, INVALID_PARAMS, "Command type does not match method")
        try:
            result = await handler(command)
        except Exception:
            logger.exception("Unhandled command error for %s", request.method)
            return make_error(request.id, INTERNAL_ERROR, "Internal error")
        return JsonRpcSuccess(id=request.id, result=result.model_dump())
