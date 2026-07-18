import asyncio
from typing import Any

from manius_code.core.events.ipc import IpcEventBroadcaster
from manius_code.core.events.models import RunStartedEvent
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


# 功能：验证两个订阅客户端都会收到相同的 Agent 事件推送，断开连接后会被清理。
# 设计：通过真实 SocketServer 与两个长连接客户端覆盖 JSON-RPC 订阅、通知推送及断线清理路径。
def test_ipc_event_broadcaster_pushes_to_multiple_subscribers_and_cleans_disconnects(free_port: int) -> None:
    # 运行真实长连接场景并验证两次广播结果。
    async def exercise() -> None:
        broadcaster = IpcEventBroadcaster()
        server = SocketServer("127.0.0.1", free_port)

        # 验证订阅请求并把当前连接交给广播器维护。
        async def subscribe(params: dict[str, Any], writer: asyncio.StreamWriter) -> dict[str, bool]:
            assert params == {"type": "event.subscribe"}
            broadcaster.subscribe(writer)
            return {"subscribed": True}

        server.register_connection_handler("event.subscribe", subscribe)
        server.add_disconnect_handler(broadcaster.unsubscribe)
        await server.start()
        first = CapturingClient("127.0.0.1", free_port)
        second = CapturingClient("127.0.0.1", free_port)
        try:
            await first.connect()
            await second.connect()
            assert (await first.send_command("event.subscribe", {"type": "event.subscribe"})).result == {"subscribed": True}
            assert (await second.send_command("event.subscribe", {"type": "event.subscribe"})).result == {"subscribed": True}

            broadcaster.handle(RunStartedEvent(run_id="run-1", goal="goal", run_dir="runs/run-1"))
            first_event, second_event = await asyncio.gather(first.events.get(), second.events.get())
            assert first_event["method"] == "event.push"
            assert second_event["params"]["run_id"] == "run-1"

            await first.close()
            await asyncio.sleep(0)
            broadcaster.handle(RunStartedEvent(run_id="run-2", goal="goal", run_dir="runs/run-2"))
            remaining_event = await asyncio.wait_for(second.events.get(), timeout=1)
            assert remaining_event["params"]["run_id"] == "run-2"
            assert len(broadcaster._writers) == 1
        finally:
            await first.close()
            await second.close()
            await server.stop()

    asyncio.run(exercise())
