import asyncio
import json
import inspect
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
from manius_code.core.tracing import TracingProvider

CommandHandler = Callable[[dict[str, Any]], Awaitable[Any]]
ConnectionHandler = Callable[[dict[str, Any], asyncio.StreamWriter], Awaitable[Any]]
DisconnectHandler = Callable[[asyncio.StreamWriter], Awaitable[None] | None]
logger = logging.getLogger(__name__)


class SocketServer:
    # 保存监听地址、端口和已注册的命令处理器。
    def __init__(self, host: str, port: int, tracer: TracingProvider | None = None) -> None:
        self._host = host
        self._port = port
        self._tracer = tracer
        self._server: asyncio.AbstractServer | None = None
        self._handlers: dict[str, CommandHandler] = {}
        self._connection_handlers: dict[str, ConnectionHandler] = {}
        self._disconnect_handlers: list[DisconnectHandler] = []

    # 为指定 JSON-RPC 方法注册异步命令处理器。
    def register(self, method: str, handler: CommandHandler) -> None:
        self._handlers[method] = handler

    # 为需要当前客户端连接的 JSON-RPC 方法注册处理器。
    def register_connection_handler(self, method: str, handler: ConnectionHandler) -> None:
        self._connection_handlers[method] = handler

    # 添加客户端连接关闭时需要执行的清理处理器。
    def add_disconnect_handler(self, handler: DisconnectHandler) -> None:
        self._disconnect_handlers.append(handler)

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
            await self._notify_disconnect(writer)
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
            client_id = self._client_id(writer)
            response = await self._make_response(line, writer, client_id)
            self._trace_response(response, client_id)
            writer.write(response.model_dump_json().encode() + b"\n")
            await writer.drain()
        except (ConnectionError, OSError):
            logger.debug("Client disconnected before receiving a response")

    # 验证请求、调用已注册处理器并封装 JSON-RPC 响应。
    async def _make_response(
        self,
        line: bytes,
        writer: asyncio.StreamWriter,
        client_id: str | None,
    ) -> JsonRpcSuccess | JsonRpcError:
        try:
            raw_request = json.loads(line)
        except json.JSONDecodeError:
            self._trace_request_parse_error(line, client_id)
            return make_error(None, PARSE_ERROR, "Parse error")
        self._trace_request(raw_request, client_id)
        try:
            request = JsonRpcRequest.model_validate(raw_request)
        except ValidationError:
            request_id = raw_request.get("id") if isinstance(raw_request, dict) else None
            return make_error(request_id, INVALID_REQUEST, "Invalid Request")
        # 匹配对应的处理器
        handler = self._handlers.get(request.method)
        connection_handler = self._connection_handlers.get(request.method)
        if handler is None and connection_handler is None:
            return make_error(request.id, METHOD_NOT_FOUND, "Method not found")
        try:
            result = await connection_handler(request.params, writer) if connection_handler else await handler(request.params)
        except ValidationError:
            return make_error(request.id, INVALID_PARAMS, "Invalid params")
        except Exception:
            logger.exception("Unhandled command error for %s", request.method)
            return make_error(request.id, INTERNAL_ERROR, "Internal error")
        return JsonRpcSuccess(id=request.id, result=self._serialize_result(result))

    # 依次通知连接关闭处理器并忽略清理过程中的异常。
    async def _notify_disconnect(self, writer: asyncio.StreamWriter) -> None:
        for handler in self._disconnect_handlers:
            try:
                result = handler(writer)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("Unhandled client disconnect handler error")

    # 将 Pydantic 返回对象转换为 JSON-RPC 可序列化结果。
    def _serialize_result(self, result: Any) -> Any:
        return result.model_dump() if isinstance(result, BaseModel) else result

    # 记录已成功解析的原始 JSON-RPC 请求并保留请求关联标识。
    def _trace_request(self, raw_request: Any, client_id: str | None) -> None:
        if self._tracer is None:
            return
        if isinstance(raw_request, dict):
            self._tracer.emit(
                "CLIENT->CORE",
                "ipc",
                "request",
                raw_request,
                client_id=client_id,
                trace_id=self._trace_id(raw_request.get("id")),
            )
            return
        self._tracer.emit(
            "CLIENT->CORE",
            "ipc",
            "invalid_request",
            {"raw": raw_request, "error": "Invalid Request"},
            client_id=client_id,
        )

    # 记录无法解析的 NDJSON 帧文本与解析错误，便于定位协议兼容问题。
    def _trace_request_parse_error(self, line: bytes, client_id: str | None) -> None:
        if self._tracer is not None:
            self._tracer.emit(
                "CLIENT->CORE",
                "ipc",
                "parse_error",
                {"raw": line.decode("utf-8", errors="replace").rstrip("\r\n"), "error": "Parse error"},
                client_id=client_id,
            )

    # 在写入 TCP 连接前记录完整的 JSON-RPC 响应帧。
    def _trace_response(self, response: JsonRpcSuccess | JsonRpcError, client_id: str | None) -> None:
        if self._tracer is None:
            return
        payload = response.model_dump(mode="json")
        result = payload.get("result")
        run_id = result.get("run_id") if isinstance(result, dict) else None
        self._tracer.emit(
            "CORE>CLIENT",
            "ipc",
            "response",
            payload,
            run_id=run_id if isinstance(run_id, str) else None,
            client_id=client_id,
            trace_id=self._trace_id(response.id),
        )

    # 将 JSON-RPC 标识标准化为追踪记录使用的可选字符串。
    def _trace_id(self, value: int | str | None) -> str | None:
        return str(value) if value is not None else None

    # 从 TCP 对端地址生成可用于关联 IPC 收发记录的客户端标识。
    def _client_id(self, writer: asyncio.StreamWriter) -> str | None:
        peername = writer.get_extra_info("peername")
        if isinstance(peername, tuple) and len(peername) >= 2:
            return f"{peername[0]}:{peername[1]}"
        return str(peername) if peername is not None else None
