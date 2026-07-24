from __future__ import annotations

import asyncio
import json
import sys
from contextlib import suppress
from typing import Any

from pydantic import TypeAdapter, ValidationError
from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Input, Static
from textual.worker import Worker

from manius_code.core.bus.commands import EventSubscribeResult, EventUnsubscribeResult, SessionCreateResult, SessionGetResult, SessionSendResult
from manius_code.core.bus.events import AgentEvent, LlmTokenEvent, NoteSavedEvent, RunFinishedEvent, SessionCreatedEvent, SessionMessageSentEvent
from manius_code.core.config import ConfigError, ManiusConfig, load_config
from manius_code.core.transport.socket_client import IpcError, SocketClient

_EVENT_ADAPTER = TypeAdapter(AgentEvent)
_RETRY_DELAY_SECONDS = 2
_TOKEN_FLUSH_INTERVAL_SECONDS = 0.08
_MANIUSCODE_LOGO = """\
███╗   ███╗ █████╗ ███╗   ██╗██╗██╗   ██╗███████╗███████╗ ██████╗ ███████╗ ███████╗
████╗ ████║██╔══██╗████╗  ██║██║██║   ██║██╔════╝██╔════╝██╔═══██╗██╔══ ██║██╔════╝
██╔████╔██║███████║██╔██╗ ██║██║██║   ██║███████╗██║     ██║   ██║██║   ██║█████╗  
██║╚██╔╝██║██╔══██║██║╚██╗██║██║██║   ██║╚════██║██║     ██║   ██║██║   ██║██╔══╝  
██║ ╚═╝ ██║██║  ██║██║ ╚████║██║╚██████╔╝███████║╚██████╗╚██████╔╝███████╔╝███████╗
╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝╚═╝ ╚═════╝ ╚══════╝ ╚═════╝ ╚═════╝ ╚══════╝ ╚══════╝
"""


# 截断长字段以保持工具调用摘要在窄终端中可读。
def _preview(value: str, limit: int = 96) -> str:
    return value if len(value) <= limit else f"{value[: limit - 3]}..."


class LlmStreamBlock(Static):
    """A single result block that is updated while the LLM is streaming."""

    DEFAULT_CSS = "LLMStreamBlock { padding: 0 2; color: $text; }"

    # 初始化一个用于累积并原地更新结果文本的流式组件。
    def __init__(self) -> None:
        super().__init__("")
        self._text = ""
        self._finalized = False

    # 追加一批 token 并更新同一组件，避免为每个 token 新建日志行。
    def append_text(self, text: str) -> None:
        if self._finalized:
            return
        self._text += text
        self.update(self._text)

    # 结束流式阶段后将完整结果升级为 Markdown 渲染。
    def finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        if self._text.strip():
            self.update(Markdown(self._text, code_theme="monokai"))


class ToolCallBlock(Static):
    """A compact tool-call row that changes state in place."""

    DEFAULT_CSS = "ToolCallBlock { padding: 0 2; color: $text-muted; }"
    can_focus = True

    # 初始化工具调用摘要及其等待完成的状态。
    def __init__(self, tool_name: str, arguments: dict[str, Any]) -> None:
        self._tool_name = tool_name
        self._arguments = arguments
        self._duration_ms: int | None = None
        self._error: str | None = None
        self._result: str | None = None
        self._expanded = False
        super().__init__(self._render_summary())

    # 根据工具状态生成一行带颜色的摘要文本。
    def _render_summary(self) -> Text:
        text = Text("  ")
        text.append("tool ", style="dim")
        text.append(self._tool_name, style="bold")
        arguments = _preview(json.dumps(self._arguments, ensure_ascii=False, default=str), 96)
        if arguments:
            text.append(f"  {arguments}", style="dim")
        if self._duration_ms is None:
            text.append("  running", style="yellow")
        elif self._error is None:
            text.append(f"  done  {self._duration_ms}ms", style="green")
        else:
            text.append(f"  failed  {self._duration_ms}ms", style="red")
            text.append(f"  {_preview(self._error)}", style="red")
        if self._expanded:
            text.append("\n    arguments: ", style="dim")
            text.append(json.dumps(self._arguments, ensure_ascii=False, default=str), style="dim")
            if self._result is not None:
                text.append("\n    output:\n", style="dim")
                text.append(self._result)
            if self._error is not None:
                text.append("\n    error: ", style="red")
                text.append(self._error, style="red")
            text.append("\n    click to collapse", style="dim")
        else:
            text.append("  click for details", style="dim")
        return text

    # 将工具调用更新为成功或失败的最终状态。
    def finish(self, duration_ms: int, error: str | None = None, result: str | None = None) -> None:
        self._duration_ms = duration_ms
        self._error = error
        self._result = result
        self.update(self._render_summary())

    # 点击摘要行时切换完整参数与工具输出的可见状态。
    def on_click(self) -> None:
        self._expanded = not self._expanded
        self.set_class(self._expanded, "expanded")
        self.update(self._render_summary())


class ManiusTui(App[None]):
    """Read-only daemon event observer built with reusable IPC components."""

    TITLE = "maniuscode"
    BINDINGS = [("q", "quit", "Quit")]
    CSS = """
    Screen { background: $background; }

    #header {
        height: 1;
        background: $surface;
        color: $text;
        padding: 0 1;
    }

    #log-view {
        height: 1fr;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
    }

    #message-input {
        height: 3;
        margin: 0 1 1 1;
    }

    #banner { padding: 1 2 0 2; }
    Static.run-header { color: $text-muted; padding: 1 2 0 2; }
    Static.step-divider { color: $text-muted; padding: 0 2; }
    Static.run-ok { color: green; padding: 0 2 1 2; }
    Static.run-err { color: red; padding: 0 2 1 2; }
    Static.log-line { padding: 0 2; }
    """

    # 保存 daemon 配置、连接、流式结果块和待完成的工具调用块。
    def __init__(self, config: ManiusConfig) -> None:
        super().__init__()
        self._config = config
        self._client: SocketClient | None = None
        self._socket_worker: Worker[None] | None = None
        self._token_buffers: dict[str, str] = {}
        self._stream_blocks: dict[str, LlmStreamBlock] = {}
        self._tool_blocks: dict[tuple[str, int, str], ToolCallBlock] = {}
        self._session_id: str | None = None
        self._session_turn_count = 0
        self._active_run_id: str | None = None

    # 组合状态栏和可滚动的组件化事件视图。
    def compose(self) -> ComposeResult:
        yield Static(id="header")
        yield VerticalScroll(id="log-view")
        yield Input(placeholder="输入目标后按 Enter 发送", disabled=True, id="message-input")

    # 挂载 Logo、启动 token 批量刷新定时器和 socket Worker。
    def on_mount(self) -> None:
        banner = Text(_MANIUSCODE_LOGO, style="bold cyan")
        banner.append("\n  MANIUSCODE  /  daemon event observer  •  press q to quit", style="dim")
        self._append(Static(banner, id="banner"))
        self._update_header("connecting")
        self.set_interval(_TOKEN_FLUSH_INTERVAL_SECONDS, self._flush_token_buffers)
        self._socket_worker = self.run_worker(self.socket_loop(), exclusive=True, name="socket")

    # 卸载时取消 Worker，由其 finally 块完成退订和连接关闭。
    def on_unmount(self) -> None:
        if self._socket_worker is not None:
            self._socket_worker.cancel()

    # 将事件组件挂入滚动视图并始终保持最新内容可见。
    def _append(self, widget: Static) -> None:
        log_view = self.query_one("#log-view", VerticalScroll)
        log_view.mount(widget)
        log_view.scroll_end(animate=False)

    # 根据连接状态更新紧凑的品牌、地址和订阅状态栏。
    def _update_header(self, state: str) -> None:
        color = {"connected": "green", "connecting": "yellow", "disconnected": "red"}[state]
        header = Text()
        header.append("maniuscode", style="bold")
        header.append(f"  {self._config.host}:{self._config.port}", style="dim")
        header.append(f"  {state}", style=color)
        if self._session_id is None:
            header.append("  session: connecting", style="dim")
        else:
            header.append(f"  session: {self._session_id[:8]}  turns: {self._session_turn_count}", style="dim")
        self.query_one("#header", Static).update(header)

    # 按当前连接状态启用或禁用会话输入框，并在可输入时恢复焦点。
    def _set_input_enabled(self, enabled: bool) -> None:
        try:
            input_box = self.query_one("#message-input", Input)
        except NoMatches:
            return
        input_box.disabled = not enabled
        if enabled:
            input_box.focus()

    # 接收输入框提交事件并交由 Textual Worker 发送会话消息，避免阻塞界面事件循环。
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "message-input" or self._session_id is None:
            return
        content = event.value.strip()
        if not content or self._active_run_id is not None:
            return
        event.input.value = ""
        self._set_input_enabled(False)
        self.run_worker(self._send_message(content), exclusive=False, name="session-send")

    # 循环连接 daemon、订阅全局事件并在断线后自动重试。
    async def socket_loop(self) -> None:
        while True:
            client = SocketClient(self._config.host, self._config.port, event_handler=self.handle_event)
            self._client = client
            sub_id: str | None = None
            event_loop_task: asyncio.Task[None] | None = None
            try:
                await client.connect()
                await self._ensure_session(client)
                subscription_response = await client.send_command(
                    "event.subscribe",
                    {"type": "event.subscribe", "run_id": None, "topics": ["*"]},
                )
                subscription = EventSubscribeResult.model_validate(subscription_response.result)
                sub_id = subscription.sub_id
                self._update_header("connected")
                self._set_input_enabled(self._active_run_id is None)
                event_loop_task = client._event_loop_task
                if event_loop_task is None:
                    raise RuntimeError("SocketClient did not start its event loop")
                await event_loop_task
            except asyncio.CancelledError:
                raise
            except (IpcError, OSError, RuntimeError, ValidationError):
                pass
            finally:
                self._finish_all_streams()
                self._set_input_enabled(False)
                try:
                    if sub_id is not None and event_loop_task is not None and not event_loop_task.done():
                        with suppress(IpcError, OSError, RuntimeError, ValidationError):
                            response = await client.send_command(
                                "event.unsubscribe",
                                {"type": "event.unsubscribe", "sub_id": sub_id},
                            )
                            EventUnsubscribeResult.model_validate(response.result)
                finally:
                    with suppress(OSError):
                        await client.close()
                    if self._client is client:
                        self._client = None
            self._update_header("disconnected")
            await asyncio.sleep(_RETRY_DELAY_SECONDS)

    # 创建默认会话或重新加载已有会话元数据，使断线重连后仍能延续同一轮对话。
    async def _ensure_session(self, client: SocketClient) -> None:
        if self._session_id is None:
            response = await client.send_command("session.create", {"type": "session.create", "client_id": "tui"})
            session = SessionCreateResult.model_validate(response.result).session
        else:
            response = await client.send_command("session.get", {"type": "session.get", "session_id": self._session_id})
            session = SessionGetResult.model_validate(response.result).session
        self._session_id = session.session_id
        self._session_turn_count = session.turn_count

    # 通过当前长连接发送会话消息，并在 RPC 失败时恢复输入能力并显示简短错误。
    async def _send_message(self, content: str) -> None:
        client = self._client
        session_id = self._session_id
        if client is None or session_id is None:
            self._append(Static("not connected to daemon", classes="run-err"))
            self._set_input_enabled(True)
            return
        try:
            response = await client.send_command(
                "session.send",
                {"type": "session.send", "session_id": session_id, "content": content},
            )
            result = SessionSendResult.model_validate(response.result)
            self._active_run_id = result.run_id
            self._append(Static(f"── turn {self._session_turn_count + 1} ──", classes="step-divider"))
        except (IpcError, OSError, RuntimeError, ValidationError) as error:
            self._append(Static(f"unable to send message: {error}", classes="run-err"))
            self._set_input_enabled(True)

    # 校验服务端事件后交由组件化渲染逻辑处理，非法事件直接忽略。
    async def handle_event(self, message: dict[str, Any]) -> None:
        try:
            event = _EVENT_ADAPTER.validate_python(message)
        except ValidationError:
            return
        self._handle_agent_event(event)

    # 按事件类型更新流式结果块、工具块或新增普通事件组件。
    def _handle_agent_event(self, event: AgentEvent) -> None:
        if isinstance(event, SessionCreatedEvent):
            if event.session_id == self._session_id:
                self._update_header("connected")
            return
        if isinstance(event, SessionMessageSentEvent):
            if event.session_id == self._session_id:
                self._active_run_id = event.run_id
            return
        if isinstance(event, NoteSavedEvent):
            if event.session_id == self._session_id:
                self._append(Static(f"  [green]saved note {event.note_id}[/green]  [dim]{event.title}[/dim]", classes="log-line"))
            return
        if self._active_run_id is not None and event.run_id != self._active_run_id:
            return
        if isinstance(event, LlmTokenEvent):
            self._token_buffers[event.run_id] = self._token_buffers.get(event.run_id, "") + event.token
            return
        self._finish_stream(event.run_id)
        match event.type:
            case "run_started":
                self._append(
                    Static(
                        f"run  [cyan]{event.run_id}[/cyan]  [dim]{_preview(event.goal)}[/dim]",
                        classes="run-header",
                    )
                )
            case "run_resumed":
                self._append(
                    Static(
                        f"resumed  [cyan]{event.run_id}[/cyan]  [dim]{_preview(event.goal)}[/dim]",
                        classes="run-header",
                    )
                )
            case "step_planning":
                self._append(Static(f"step {event.step}  [cyan]{event.plan}[/cyan]", classes="step-divider"))
            case "step_done":
                if not event.complete:
                    self._append(Static(f"  [dim]{_preview(event.observation)}[/dim]", classes="log-line"))
            case "tool_call_start":
                block = ToolCallBlock(event.tool_name, event.arguments)
                self._tool_blocks[(event.run_id, event.step, event.tool_name)] = block
                self._append(block)
            case "tool_call_success":
                self._finish_tool(event.run_id, event.step, event.tool_name, event.duration_ms, result=event.result)
            case "tool_call_failed":
                self._finish_tool(event.run_id, event.step, event.tool_name, event.duration_ms, event.error)
            case "run_finished":
                self._append(self._run_finished_widget(event))
                if event.run_id == self._active_run_id:
                    self._active_run_id = None
                    self._session_turn_count += 1
                    self._update_header("connected")
                    self._set_input_enabled(True)

    # 将同一任务当前缓冲的 token 追加到唯一的流式结果组件。
    def _flush_token_buffers(self) -> None:
        buffers = self._token_buffers
        self._token_buffers = {}
        for run_id, text in buffers.items():
            if not text:
                continue
            block = self._stream_blocks.get(run_id)
            if block is None:
                block = LlmStreamBlock()
                self._stream_blocks[run_id] = block
                self._append(block)
            block.append_text(text)
        if buffers:
            self.query_one("#log-view", VerticalScroll).scroll_end(animate=False)

    # 完成指定任务的流式结果块并在后续事件前进行 Markdown 渲染。
    def _finish_stream(self, run_id: str) -> None:
        self._flush_token_buffers()
        block = self._stream_blocks.pop(run_id, None)
        if block is not None:
            block.finalize()

    # 连接关闭或应用退出时完成全部尚未结束的流式结果。
    def _finish_all_streams(self) -> None:
        self._flush_token_buffers()
        for block in self._stream_blocks.values():
            block.finalize()
        self._stream_blocks.clear()

    # 将指定工具调用块更新为成功或失败状态。
    def _finish_tool(
        self,
        run_id: str,
        step: int,
        tool_name: str,
        duration_ms: int,
        error: str | None = None,
        result: str | None = None,
    ) -> None:
        block = self._tool_blocks.pop((run_id, step, tool_name), None)
        if block is None:
            status = "failed" if error else "done"
            detail = f": {error}" if error else ""
            self._append(Static(f"  tool {tool_name}  {status}  {duration_ms}ms{detail}", classes="log-line"))
            return
        block.finish(duration_ms, error, result)

    # 生成任务成功或失败的收尾组件。
    def _run_finished_widget(self, event: RunFinishedEvent) -> Static:
        if event.status == "success":
            return Static(
                f"completed  [dim]{event.total_steps} steps  {event.duration_ms}ms[/dim]",
                classes="run-ok",
            )
        reason = f"  [dim]{event.reason}[/dim]" if event.reason else ""
        return Static(
            f"failed{reason}  [dim]{event.total_steps} steps  {event.duration_ms}ms[/dim]",
            classes="run-err",
        )


# 加载配置并启动常驻的 Textual 观测客户端。
def main() -> None:
    try:
        config = load_config()
    except ConfigError as error:
        print(f"manius-tui: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    ManiusTui(config).run()
