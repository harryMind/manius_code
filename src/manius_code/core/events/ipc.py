import asyncio
import logging
from dataclasses import dataclass
from fnmatch import fnmatchcase
from uuid import uuid4

from manius_code.core.bus.events import AgentEvent, EventPushEnvelope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventSubscription:
    sub_id: str
    writer: asyncio.StreamWriter
    run_id: str | None
    topics: tuple[str, ...]

    # 判断事件是否符合订阅的运行范围和 topic 过滤规则。
    def matches(self, event: AgentEvent) -> bool:
        return (self.run_id is None or self.run_id == event.run_id) and any(fnmatchcase(event.type, topic) for topic in self.topics)


class IpcEventBroadcaster:
    # 初始化订阅记录与待完成推送任务的集合。
    def __init__(self) -> None:
        self._subscriptions: dict[str, EventSubscription] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # 创建带运行范围和 topic 规则的事件订阅并返回订阅标识。
    def subscribe(self, writer: asyncio.StreamWriter, run_id: str | None = None, topics: list[str] | None = None) -> str:
        sub_id = uuid4().hex
        self._subscriptions[sub_id] = EventSubscription(
            sub_id,
            writer,
            run_id,
            tuple(topics if topics is not None else ["*"]),
        )
        return sub_id

    # 按订阅标识主动取消事件订阅。
    def unsubscribe(self, sub_id: str) -> bool:
        return self._subscriptions.pop(sub_id, None) is not None

    # 清理指定客户端连接创建的全部订阅。
    def unsubscribe_writer(self, writer: asyncio.StreamWriter) -> None:
        for sub_id, subscription in tuple(self._subscriptions.items()):
            if subscription.writer is writer:
                self.unsubscribe(sub_id)

    # 为匹配范围和 topic 的订阅异步调度事件推送而不阻塞 EventBus。
    def handle(self, event: AgentEvent) -> None:
        for subscription in tuple(self._subscriptions.values()):
            if not subscription.matches(event):
                continue
            task = asyncio.create_task(self._send(subscription, event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    # 以标准 JSON-RPC 通知信封向订阅连接推送事件。
    async def _send(self, subscription: EventSubscription, event: AgentEvent) -> None:
        try:
            subscription.writer.write(EventPushEnvelope(params=event).model_dump_json().encode() + b"\n")
            await subscription.writer.drain()
        except (ConnectionError, OSError, RuntimeError):
            logger.debug("Removing disconnected event subscriber")
            self.unsubscribe(subscription.sub_id)
