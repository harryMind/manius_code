import inspect
from collections.abc import Awaitable, Callable

from manius_code.core.events.models import AgentEvent

Subscriber = Callable[[AgentEvent], Awaitable[None] | None]


class EventBus:
    # 初始化空的事件订阅者列表。
    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    # 添加一个接收所有 Agent 事件的订阅者。
    def subscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.append(subscriber)

    # 按注册顺序向全部订阅者广播事件。
    async def publish(self, event: AgentEvent) -> None:
        for subscriber in self._subscribers:
            result = subscriber(event)
            if inspect.isawaitable(result):
                await result
