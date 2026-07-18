import asyncio
from typing import Any

import pytest
from rich.text import Text

from manius_code.core.bus.events import LlmResponseEvent, LlmTokenEvent, RunFinishedEvent, RunStartedEvent, StepPlanningEvent
from manius_code.core.config import ManiusConfig
from manius_code.core.events.ipc import IpcEventBroadcaster
from manius_code.core.transport.socket_server import SocketServer
from manius_code.tui.app import _MANIUSCODE_LOGO, _TOKEN_FLUSH_INTERVAL_SECONDS, ManiusTui
from textual.widgets import RichLog, Static


# 功能：验证 TUI 将事件按 run_id 和状态生成带语义标签的富文本。
# 设计：直接调用纯格式化逻辑，不依赖终端驱动即可覆盖多任务区分和成功完成样式。
def test_tui_formats_events_with_run_id_and_status() -> None:
    app = ManiusTui(ManiusConfig())
    started = app._format_event(RunStartedEvent(run_id="run-a", goal="inspect README", run_dir="runs/run-a"))
    finished = app._format_event(
        RunFinishedEvent(run_id="run-b", status="success", total_steps=1, duration_ms=42, summary="done")
    )

    assert started.plain == "[run-a] RUN inspect README"
    assert finished.plain == "[run-b] FINISHED 42ms"


# 功能：验证 TUI 顶部包含 ManiusCode ASCII 品牌标志和守护进程地址。
# 设计：挂载实际 Textual 应用后按组件 ID 查询，覆盖静态布局而不依赖终端截图比对。
def test_tui_displays_maniuscode_logo_and_daemon_address() -> None:
    # 驱动应用挂载并校验品牌与连接信息组件。
    async def exercise() -> None:
        app = ManiusTui(ManiusConfig(host="127.0.0.1", port=7437))
        async with app.run_test():
            assert _MANIUSCODE_LOGO.startswith("M   M")
            assert app.query_one("#logo", Static) is not None
            assert app.query_one("#daemon-address", Static).render() == "127.0.0.1:7437"

    asyncio.run(exercise())


# 功能：验证 LLM token 只在后续非 token 事件到达时批量写入日志。
# 设计：使用 Textual 测试驱动挂载真实 RichLog，断言 token 阶段无写入且刷新后缓冲被清空。
def test_tui_buffers_tokens_until_a_non_token_event_arrives() -> None:
    # 驱动挂载后的 TUI 接收 token 与普通事件。
    async def exercise() -> None:
        app = ManiusTui(ManiusConfig(port=1))
        async with app.run_test():
            log = app.query_one("#event-log", RichLog)
            initial_lines = len(log.lines)
            await app.handle_event(LlmTokenEvent(run_id="run-a", token="hello").model_dump(mode="json"))
            assert app._token_buffer == "hello"
            assert len(log.lines) == initial_lines
            await app.handle_event(StepPlanningEvent(run_id="run-a", step=1, plan="continue").model_dump(mode="json"))
            assert app._token_buffer == ""
            assert len(log.lines) >= initial_lines + 2

    asyncio.run(exercise())


# 功能：验证 TUI 定时批量刷新 token，实现不等待步骤事件的连续流式展示。
# 设计：在真实 Textual 定时器环境中等待两个刷新周期，断言缓冲被自动清空且日志已追加。
def test_tui_periodically_flushes_plain_token_results(monkeypatch: pytest.MonkeyPatch) -> None:
    # 驱动 TUI 定时器批量写入 LLM token 缓冲。
    async def exercise() -> None:
        app = ManiusTui(ManiusConfig(port=1))
        async with app.run_test() as pilot:
            log = app.query_one("#event-log", RichLog)
            captured: list[Text] = []
            original_write = RichLog.write

            # 收集真实 RichLog 写入内容以校验不混入内部 LLM 事件标签。
            def capture_write(widget: RichLog, content: Text, *args: Any, **kwargs: Any) -> RichLog:
                if isinstance(content, Text):
                    captured.append(content)
                return original_write(widget, content, *args, **kwargs)

            monkeypatch.setattr(RichLog, "write", capture_write)
            await app.handle_event(LlmTokenEvent(run_id="run-a", token="streaming").model_dump(mode="json"))
            await asyncio.sleep(_TOKEN_FLUSH_INTERVAL_SECONDS * 2)
            await pilot.pause()
            assert app._token_buffer == ""
            await app.handle_event(
                LlmResponseEvent(run_id="run-a", duration_ms=1, text="streaming", tool_calls=[]).model_dump(mode="json")
            )
            assert [content.plain for content in captured] == ["streaming"]

    asyncio.run(exercise())


# 功能：验证 TUI Worker 通过既有 SocketClient 订阅全局事件并渲染服务端推送。
# 设计：启动真实 SocketServer 和 IpcEventBroadcaster，覆盖连接、订阅、event.push 解包和 RichLog 写入链路。
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
                assert len(app.query_one("#event-log", RichLog).lines) > 0
        finally:
            await server.stop()

    asyncio.run(exercise())
