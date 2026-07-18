import asyncio
from typing import Any

import pytest

from manius_code.cli.commands.run import _run_remote, run
from manius_code.core.bus.commands import AgentRunResult, EventListResult, EventSubscribeResult, EventUnsubscribeResult
from manius_code.core.bus.events import RunFinishedEvent, RunStartedEvent
from manius_code.core.config import ManiusConfig
from manius_code.core.events.ipc import IpcEventBroadcaster
from manius_code.core.transport.socket_client import IpcError
from manius_code.core.transport.socket_server import SocketServer


# 功能：验证 CLI 以 run_id 回放历史、订阅实时事件并通过完成事件结束远程运行。
# 设计：模拟 agent.run 立即返回、后续异步广播完成事件，覆盖任务 ID 闭环和订阅竞态。
def test_cli_remote_run_replays_history_and_consumes_scoped_completion_event(free_port: int) -> None:
    # 驱动模拟 daemon 与 CLI 客户端完成一次异步远程运行。
    async def exercise() -> RunFinishedEvent:
        broadcaster = IpcEventBroadcaster()
        history: dict[str, list[dict[str, Any]]] = {}
        server = SocketServer("127.0.0.1", free_port)

        # 按 run_id 和 topic 创建实时事件订阅。
        async def subscribe(params: dict[str, Any], writer: asyncio.StreamWriter) -> dict[str, Any]:
            sub_id = broadcaster.subscribe(writer, params.get("run_id"), params.get("topics"))
            return EventSubscribeResult(sub_id=sub_id, run_id=params.get("run_id"), topics=params.get("topics", ["*"])).model_dump()

        # 取消 CLI 完成后持有的事件订阅。
        async def unsubscribe(params: dict[str, Any]) -> dict[str, bool]:
            return EventUnsubscribeResult(unsubscribed=broadcaster.unsubscribe(params["sub_id"])).model_dump()

        # 返回指定任务已经持久化的事件历史。
        async def list_events(params: dict[str, Any]) -> dict[str, Any]:
            run_id = params["run_id"]
            return EventListResult(run_id=run_id, events=history.get(run_id, [])).model_dump()

        # 立即返回任务标识，并在订阅建立后异步推送结束事件。
        async def run_agent(params: dict[str, Any]) -> AgentRunResult:
            assert params == {"type": "agent.run", "goal": "remote goal"}
            run_id = "remote-run"
            started = RunStartedEvent(run_id=run_id, goal="remote goal", run_dir="runs/remote-run")
            history[run_id] = [started.model_dump(mode="json")]

            async def finish() -> None:
                await asyncio.sleep(0.01)
                completed = RunFinishedEvent(run_id=run_id, status="success", total_steps=1, duration_ms=1, summary="done")
                history[run_id].append(completed.model_dump(mode="json"))
                broadcaster.handle(completed)

            asyncio.create_task(finish())
            return AgentRunResult(run_id=run_id)

        server.register_connection_handler("event.subscribe", subscribe)
        server.register("event.unsubscribe", unsubscribe)
        server.register("event.list", list_events)
        server.register("agent.run", run_agent)
        server.add_disconnect_handler(broadcaster.unsubscribe_writer)
        await server.start()
        try:
            return await _run_remote(ManiusConfig(port=free_port), "remote goal")
        finally:
            await server.stop()

    finished_event = asyncio.run(exercise())
    assert finished_event.run_id == "remote-run"
    assert finished_event.status == "success"


# 功能：验证 CLI 将远程 IPC 错误转换为清晰提示和非零退出码。
# 设计：替换异步远程执行函数直接抛出协议错误，避免依赖真实服务端并覆盖命令行错误边界。
def test_cli_run_reports_ipc_errors_without_traceback(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # 模拟 daemon 返回的 JSON-RPC 失败响应。
    async def fail_remote(config: ManiusConfig, goal: str) -> RunFinishedEvent:
        raise IpcError(-32601, "Method not found")

    monkeypatch.setattr("manius_code.cli.commands.run._run_remote", fail_remote)
    with pytest.raises(SystemExit) as error:
        run(ManiusConfig(), "remote goal")

    assert error.value.code == 1
    assert capsys.readouterr().err == "manius: IPC request failed: [-32601] Method not found\n"
