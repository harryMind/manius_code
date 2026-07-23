import asyncio
from typing import Any

import pytest

from manius_code.cli.commands.run import _resume_remote, _run_remote, run
from manius_code.core.bus.commands import AgentRunResult, EventListResult, EventSubscribeResult, EventUnsubscribeResult
from manius_code.core.bus.events import LlmRequestEvent, RunFinishedEvent, RunStartedEvent
from manius_code.core.config import ManiusConfig
from manius_code.core.events.ipc import IpcEventBroadcaster
from manius_code.core.transport.socket_client import IpcError
from manius_code.core.transport.socket_server import SocketServer


# 功能：验证 CLI 可接收超过默认读取上限的事件，随后通过完成事件结束远程运行。
# 设计：模拟大尺寸 llm_request 与异步完成推送，覆盖 IPC 帧大小、历史回放和订阅等待的组合边界。
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
                request = LlmRequestEvent(
                    run_id=run_id,
                    step=1,
                    messages=[{"role": "user", "content": "x" * 100_000}],
                )
                history[run_id].append(request.model_dump(mode="json"))
                broadcaster.handle(request)
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
            return await asyncio.wait_for(_run_remote(ManiusConfig(port=free_port), "remote goal"), timeout=2)
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


# 功能：验证事件流在任务完成前断开时，CLI 会退出并报告 IPC 错误而非永久等待。
# 设计：真实服务端在建立订阅后主动关闭连接，直接覆盖完成信号永远不会到达的等待边界。
def test_cli_remote_run_fails_when_event_stream_closes_before_completion(free_port: int) -> None:
    # 构造订阅成功后立即断开的 daemon 替身。
    async def exercise() -> None:
        server = SocketServer("127.0.0.1", free_port)

        # 返回待观察任务的固定运行标识。
        async def run_agent(params: dict[str, Any]) -> AgentRunResult:
            return AgentRunResult(run_id="closing-run")

        # 返回尚无完成事件的空历史记录。
        async def list_events(params: dict[str, Any]) -> dict[str, Any]:
            return EventListResult(run_id=params["run_id"], events=[]).model_dump()

        # 确认订阅后关闭当前连接，模拟中断的事件流。
        async def subscribe(params: dict[str, Any], writer: asyncio.StreamWriter) -> dict[str, Any]:
            # 延迟关闭写入端以确保订阅响应先被客户端接收。
            async def close_connection() -> None:
                await asyncio.sleep(0.01)
                writer.close()
                await writer.wait_closed()

            asyncio.create_task(close_connection())
            return EventSubscribeResult(
                sub_id="closing-subscription",
                run_id=params["run_id"],
                topics=params["topics"],
            ).model_dump()

        server.register("agent.run", run_agent)
        server.register("event.list", list_events)
        server.register_connection_handler("event.subscribe", subscribe)
        await server.start()
        try:
            with pytest.raises(IpcError, match="Event stream closed before run finished"):
                await asyncio.wait_for(_run_remote(ManiusConfig(port=free_port), "remote goal"), timeout=2)
        finally:
            await server.stop()

    asyncio.run(exercise())


# 功能：验证 CLI resume 会发送 agent.resume、订阅相同 run_id 并消费恢复后的完成事件。
# 设计：使用最小 JSON-RPC 服务替身同时检查恢复参数与订阅范围，避免仅复用 run 测试而遗漏新命令协议。
def test_cli_remote_resume_observes_the_resumed_run(free_port: int) -> None:
    # 启动一个在订阅建立后发送恢复完成事件的本地服务端。
    async def exercise() -> RunFinishedEvent:
        broadcaster = IpcEventBroadcaster()
        history: dict[str, list[dict[str, Any]]] = {}
        server = SocketServer("127.0.0.1", free_port)

        # 返回恢复任务已有的 run_resumed 历史事件与目标任务标识。
        async def resume_agent(params: dict[str, Any]) -> AgentRunResult:
            assert params == {"type": "agent.resume", "run_id": "stopped-run"}
            history["stopped-run"] = [
                RunStartedEvent(run_id="stopped-run", goal="saved goal", run_dir="runs/stopped-run").model_dump(mode="json")
            ]
            return AgentRunResult(run_id="stopped-run")

        # 返回指定任务到当前时刻的历史事件。
        async def list_events(params: dict[str, Any]) -> dict[str, Any]:
            return EventListResult(run_id=params["run_id"], events=history.get(params["run_id"], [])).model_dump()

        # 建立精确订阅后广播同一运行的完成事件。
        async def subscribe(params: dict[str, Any], writer: asyncio.StreamWriter) -> dict[str, Any]:
            assert params["run_id"] == "stopped-run"
            sub_id = broadcaster.subscribe(writer, params["run_id"], params["topics"])

            # 将完成事件留给实时推送路径，以覆盖恢复后的等待逻辑。
            async def finish() -> None:
                await asyncio.sleep(0.01)
                completed = RunFinishedEvent(
                    run_id="stopped-run",
                    status="success",
                    total_steps=2,
                    duration_ms=1,
                    summary="resumed",
                )
                history["stopped-run"].append(completed.model_dump(mode="json"))
                broadcaster.handle(completed)

            asyncio.create_task(finish())
            return EventSubscribeResult(sub_id=sub_id, run_id=params["run_id"], topics=params["topics"]).model_dump()

        # 确认客户端 finally 块的退订请求能够获得合法 JSON-RPC 响应。
        async def unsubscribe(_params: dict[str, Any]) -> dict[str, bool]:
            return EventUnsubscribeResult(unsubscribed=True).model_dump()

        server.register("agent.resume", resume_agent)
        server.register("event.list", list_events)
        server.register_connection_handler("event.subscribe", subscribe)
        server.register("event.unsubscribe", unsubscribe)
        await server.start()
        try:
            return await asyncio.wait_for(_resume_remote(ManiusConfig(port=free_port), "stopped-run"), timeout=2)
        finally:
            await server.stop()

    finished = asyncio.run(exercise())

    assert finished.run_id == "stopped-run"
    assert finished.status == "success"
