import asyncio
from typing import Any

from manius_code.cli.commands.run import _run_remote
from manius_code.core.bus.commands import AgentRunResult
from manius_code.core.config import ManiusConfig
from manius_code.core.events.ipc import IpcEventBroadcaster
from manius_code.core.bus.events import RunFinishedEvent, RunStartedEvent
from manius_code.core.transport.socket_server import SocketServer


# 功能：验证 CLI 会先订阅事件、远程发起任务，并等待匹配运行标识的完成事件。
# 设计：使用真实 SocketServer 模拟事件先于 RPC 响应到达的时序，覆盖客户端的事件缓冲逻辑。
def test_cli_remote_run_consumes_pushed_events_before_waiting_for_completion(free_port: int) -> None:
    # 驱动模拟 daemon 与 CLI 客户端完成一次远程运行。
    async def exercise() -> RunFinishedEvent:
        broadcaster = IpcEventBroadcaster()
        server = SocketServer("127.0.0.1", free_port)

        # 保存订阅连接以便把运行事件推送给 CLI。
        async def subscribe(params: dict[str, Any], writer: asyncio.StreamWriter) -> dict[str, bool]:
            assert params == {"type": "event.subscribe"}
            broadcaster.subscribe(writer)
            return {"subscribed": True}

        # 在返回启动结果前发布事件以验证客户端会缓存完成状态。
        async def run_agent(params: dict[str, Any]) -> AgentRunResult:
            assert params == {"type": "agent.run", "goal": "remote goal"}
            run_id = "remote-run"
            broadcaster.handle(RunStartedEvent(run_id=run_id, goal="remote goal", run_dir="runs/remote-run"))
            broadcaster.handle(
                RunFinishedEvent(
                    run_id=run_id,
                    status="success",
                    total_steps=1,
                    duration_ms=1,
                    summary="done",
                )
            )
            return AgentRunResult(run_id=run_id)

        server.register_connection_handler("event.subscribe", subscribe)
        server.register("agent.run", run_agent)
        server.add_disconnect_handler(broadcaster.unsubscribe)
        await server.start()
        try:
            return await _run_remote(ManiusConfig(port=free_port), "remote goal")
        finally:
            await server.stop()

    finished_event = asyncio.run(exercise())
    assert finished_event.run_id == "remote-run"
    assert finished_event.status == "success"
