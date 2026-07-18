import asyncio
from typing import Any

from manius_code.core.bus.events import LlmTokenEvent, RunFinishedEvent, RunStartedEvent, StepPlanningEvent, ToolCallStartEvent, ToolCallSuccessEvent
from manius_code.core.config import ManiusConfig
from manius_code.core.events.ipc import IpcEventBroadcaster
from manius_code.core.transport.socket_server import SocketServer
from manius_code.tui.app import LlmStreamBlock, ManiusTui, ToolCallBlock, _MANIUSCODE_LOGO
from textual.containers import VerticalScroll
from textual.widgets import Static


# 功能：验证 TUI 顶部展示 ManiusCode 品牌标志、地址和滚动事件容器。
# 设计：挂载真实 Textual 应用后按组件 ID 查询，覆盖重构后的视觉骨架而不依赖终端截图。
def test_tui_displays_brand_header_and_scrollable_event_view() -> None:
    # 驱动应用挂载并校验品牌和滚动日志容器。
    async def exercise() -> None:
        app = ManiusTui(ManiusConfig(host="127.0.0.1", port=7437))
        async with app.run_test():
            assert _MANIUSCODE_LOGO.startswith("M   M")
            assert app.query_one("#banner", Static) is not None
            assert app.query_one("#header", Static).render() == "maniuscode  127.0.0.1:7437  connecting  global"
            assert app.query_one("#log-view", VerticalScroll) is not None

    asyncio.run(exercise())


# 功能：验证多个 token 批次更新同一结果组件，并在步骤事件到来时完成 Markdown 渲染。
# 设计：直接调用事件入口并检查流式组件对象身份，确保不会将 token 拆分为多个独立日志行。
def test_tui_updates_one_stream_block_per_result() -> None:
    # 驱动同一任务的连续 token 与后续步骤事件。
    async def exercise() -> None:
        app = ManiusTui(ManiusConfig(port=1))
        async with app.run_test():
            await app.handle_event(LlmTokenEvent(run_id="run-a", token="hello ").model_dump(mode="json"))
            app._flush_token_buffers()
            block = app._stream_blocks["run-a"]
            await app.handle_event(LlmTokenEvent(run_id="run-a", token="world").model_dump(mode="json"))
            app._flush_token_buffers()
            assert app._stream_blocks["run-a"] is block
            assert block._text == "hello world"
            await app.handle_event(StepPlanningEvent(run_id="run-a", step=1, plan="continue").model_dump(mode="json"))
            assert "run-a" not in app._stream_blocks

    asyncio.run(exercise())


# 功能：验证工具开始和成功事件更新同一工具调用组件，而非追加两条无关联日志。
# 设计：通过组件对象身份和结束状态断言，覆盖参考实现的原地状态更新交互模式。
def test_tui_updates_tool_call_block_in_place() -> None:
    # 驱动单次工具调用从运行中到成功完成。
    async def exercise() -> None:
        app = ManiusTui(ManiusConfig(port=1))
        async with app.run_test():
            await app.handle_event(
                ToolCallStartEvent(run_id="run-a", tool_name="read_file", arguments={"path": "README.md"}).model_dump(mode="json")
            )
            block = app._tool_blocks[("run-a", "read_file")]
            await app.handle_event(
                ToolCallSuccessEvent(run_id="run-a", tool_name="read_file", duration_ms=5, result="content").model_dump(mode="json")
            )
            assert ("run-a", "read_file") not in app._tool_blocks
            assert block._duration_ms == 5
            assert block._error is None

    asyncio.run(exercise())


# 功能：验证 TUI Worker 通过既有 SocketClient 订阅全局事件并挂入运行事件组件。
# 设计：启动真实 SocketServer 和 IpcEventBroadcaster，覆盖连接、订阅、event.push 解包和组件挂载链路。
def test_tui_worker_subscribes_and_consumes_global_events(free_port: int) -> None:
    # 驱动 daemon 与 Textual 应用完成一次全局事件推送。
    async def exercise() -> None:
        broadcaster = IpcEventBroadcaster()
        subscribed = asyncio.Event()
        server = SocketServer("127.0.0.1", free_port)

        # 为测试服务器注册与生产一致的全局订阅处理器。
        async def subscribe(params: dict[str, Any], writer: asyncio.StreamWriter) -> dict[str, Any]:
            assert params == {"type": "event.subscribe", "run_id": None, "topics": ["*"]}
            subscribed.set()
            return {"subscribed": True, "sub_id": broadcaster.subscribe(writer, None, ["*"]), "run_id": None, "topics": ["*"]}

        server.register_connection_handler("event.subscribe", subscribe)
        server.add_disconnect_handler(broadcaster.unsubscribe_writer)
        await server.start()
        app = ManiusTui(ManiusConfig(port=free_port))
        try:
            async with app.run_test() as pilot:
                await asyncio.wait_for(subscribed.wait(), timeout=1)
                broadcaster.handle(RunStartedEvent(run_id="run-a", goal="inspect README", run_dir="runs/run-a"))
                await pilot.pause()
                assert len(app.query_one("#log-view", VerticalScroll).children) >= 2
        finally:
            await server.stop()

    asyncio.run(exercise())
