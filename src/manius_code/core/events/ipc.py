import asyncio
import json
import logging

from manius_code.core.events.models import AgentEvent

logger = logging.getLogger(__name__)


class IpcEventBroadcaster:
    # 初始化订阅连接与待完成推送任务的集合。
    def __init__(self) -> None:
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    # 添加一个接收后续 Agent 事件的客户端连接。
    def subscribe(self, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)

    # 移除已断开或推送失败的客户端连接。
    def unsubscribe(self, writer: asyncio.StreamWriter) -> None:
        self._writers.discard(writer)

    # 为每个订阅连接异步调度事件推送而不阻塞 EventBus。
    def handle(self, event: AgentEvent) -> None:
        for writer in tuple(self._writers):
            task = asyncio.create_task(self._send(writer, event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    # 将事件封装为 JSON-RPC 通知并写入指定连接。
    async def _send(self, writer: asyncio.StreamWriter, event: AgentEvent) -> None:
        message = {"jsonrpc": "2.0", "method": "event.push", "params": event.model_dump(mode="json")}
        try:
            writer.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
            await writer.drain()
        except (ConnectionError, OSError, RuntimeError):
            logger.debug("Removing disconnected event subscriber")
            self.unsubscribe(writer)
