import asyncio
from typing import Any

import pytest

from manius_code.core.bus.commands import EventSubscribeResult, EventUnsubscribeResult
from manius_code.core.bus.events import StepPlanningEvent, ToolCallStartEvent
from manius_code.core.events.ipc import IpcEventBroadcaster
from manius_code.core.transport.socket_client import SocketClient
from manius_code.core.transport.socket_server import SocketServer


class CapturingClient(SocketClient):
    # 初始化用于收集服务端事件通知的异步队列。
    def __init__(self, host: str, port: int) -> None:
        super().__init__(host, port)
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    # 将服务端事件通知放入队列供测试断言。
    async def on_event(self, event: dict[str, Any]) -> None:
        await self.events.put(event)


# 功能：验证 run_id 范围、topic 过滤和主动退订共同隔离多客户端事件流。
# 设计：通过真实长连接和 JSON-RPC 通知验证标准推送信封经 SocketClient 解包后仍保留事件体。
def test_ipc_event_broadcaster_scopes_filters_and_unsubscribes(free_port: int) -> None:
    # 运行两个订阅范围不同的客户端并断言各自只收到匹配事件。
    async def exercise() -> None:
        broadcaster = IpcEventBroadcaster()
        server = SocketServer("127.0.0.1", free_port)

        # 根据订阅请求创建带运行范围和 topic 规则的广播订阅。
        async def subscribe(params: dict[str, Any], writer: asyncio.StreamWriter) -> dict[str, Any]:
            subscription = broadcaster.subscribe(writer, params.get("run_id"), params.get("topics"))
            return EventSubscribeResult(sub_id=subscription, run_id=params.get("run_id"), topics=params.get("topics", ["*"])).model_dump()

        # 根据订阅标识主动取消单个事件流。
        async def unsubscribe(params: dict[str, Any]) -> dict[str, bool]:
            return EventUnsubscribeResult(unsubscribed=broadcaster.unsubscribe(params["sub_id"])).model_dump()

        server.register_connection_handler("event.subscribe", subscribe)
        server.register("event.unsubscribe", unsubscribe)
        server.add_disconnect_handler(broadcaster.unsubscribe_writer)
        await server.start()
        first = CapturingClient("127.0.0.1", free_port)
        second = CapturingClient("127.0.0.1", free_port)
        try:
            await first.connect()
            await second.connect()
            first_subscription = await first.send_command(
                "event.subscribe", {"type": "event.subscribe", "run_id": "run-1", "topics": ["step_*"]}
            )
            await second.send_command(
                "event.subscribe", {"type": "event.subscribe", "run_id": "run-1", "topics": ["tool_*"]}
            )

            broadcaster.handle(StepPlanningEvent(run_id="run-1", step=1, plan="plan"))
            first_event = await asyncio.wait_for(first.events.get(), timeout=1)
            assert first_event["kind"] == "event"
            assert first_event["type"] == "step_planning"
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(second.events.get(), timeout=0.05)

            broadcaster.handle(ToolCallStartEvent(run_id="run-1", step=1, tool_name="read_file", arguments={}))
            second_event = await asyncio.wait_for(second.events.get(), timeout=1)
            assert second_event["type"] == "tool_call_start"

            await first.send_command("event.unsubscribe", {"type": "event.unsubscribe", "sub_id": first_subscription.result["sub_id"]})
            broadcaster.handle(StepPlanningEvent(run_id="run-1", step=2, plan="next"))
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(first.events.get(), timeout=0.05)
        finally:
            await first.close()
            await second.close()
            await server.stop()

    asyncio.run(exercise())
