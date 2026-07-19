import inspect
from collections.abc import Awaitable, Callable

from manius_code.core.bus.events import AgentEvent
from manius_code.core.tracing import TracingProvider

Subscriber = Callable[[AgentEvent], Awaitable[None] | None]


class EventBus:
    # 初始化空的事件订阅者列表。
    def __init__(self, tracer: TracingProvider | None = None) -> None:
        self._subscribers: list[Subscriber] = []
        self._tracer = tracer

    # 添加一个接收所有 Agent 事件的订阅者。
    def subscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.append(subscriber)

    # 按注册顺序向全部订阅者广播事件。
    async def publish(self, event: AgentEvent) -> None:
        if self._tracer is not None:
            self._tracer.emit(
                "core_event",
                event.model_dump(mode="json"),
                run_id=event.run_id,
            )
        for subscriber in self._subscribers:
            result = subscriber(event)
            if inspect.isawaitable(result):
                await result
