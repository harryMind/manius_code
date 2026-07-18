from __future__ import annotations

import asyncio
import json
import sys
from contextlib import suppress
from typing import Any

from pydantic import TypeAdapter, ValidationError
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import RichLog, Static
from textual.worker import Worker

from manius_code.core.bus.commands import EventSubscribeResult, EventUnsubscribeResult
from manius_code.core.bus.events import AgentEvent, LlmTokenEvent, RunFinishedEvent
from manius_code.core.config import ConfigError, ManiusConfig, load_config
from manius_code.core.transport.socket_client import IpcError, SocketClient

_EVENT_ADAPTER = TypeAdapter(AgentEvent)
_RETRY_DELAY_SECONDS = 2


class ManiusTui(App[None]):
    """Observe all daemon Agent events through a reconnecting socket client."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #status-bar {
        height: 1;
        padding: 0 1;
        background: $surface;
    }

    #connection-status {
        width: 1fr;
    }

    #subscription-mode {
        width: auto;
        text-align: right;
        color: $text-muted;
    }

    #event-log {
        height: 1fr;
    }
    """

    BINDINGS = [("q", "quit", "Quit")]

    # 保存 daemon 配置、当前连接及流式文本缓冲状态。
    def __init__(self, config: ManiusConfig) -> None:
        super().__init__()
        self._config = config
        self._client: SocketClient | None = None
        self._socket_worker: Worker[None] | None = None
        self._token_buffer = ""
        self._token_run_id: str | None = None

    # 组合固定状态栏和可滚动的富文本事件日志区域。
    def compose(self) -> ComposeResult:
        with Horizontal(id="status-bar"):
            yield Static(id="connection-status")
            yield Static("global", id="subscription-mode")
        yield RichLog(id="event-log", auto_scroll=True, markup=False, wrap=True)

    # 挂载后使用 Textual Worker 管理常驻 socket 连接循环。
    def on_mount(self) -> None:
        self._set_disconnected_status()
        self.query_one("#event-log", RichLog).focus()
        self._socket_worker = self.run_worker(self.socket_loop(), exclusive=True, name="socket")

    # 卸载时取消 Worker，由其 finally 块完成退订和连接关闭。
    def on_unmount(self) -> None:
        if self._socket_worker is not None:
            self._socket_worker.cancel()

    # 循环连接 daemon、订阅全局事件并在断线后自动重试。
    async def socket_loop(self) -> None:
        while True:
            client = SocketClient(self._config.host, self._config.port, event_handler=self.handle_event)
            self._client = client
            sub_id: str | None = None
            event_loop_task: asyncio.Task[None] | None = None
            try:
                await client.connect()
                self._set_connected_status()
                subscription_response = await client.send_command(
                    "event.subscribe",
                    {"type": "event.subscribe", "run_id": None, "topics": ["*"]},
                )
                subscription = EventSubscribeResult.model_validate(subscription_response.result)
                sub_id = subscription.sub_id
                event_loop_task = client._event_loop_task
                if event_loop_task is None:
                    raise RuntimeError("SocketClient did not start its event loop")
                await event_loop_task
            except asyncio.CancelledError:
                raise
            except (IpcError, OSError, RuntimeError, ValidationError):
                pass
            finally:
                self._flush_token_buffer()
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
            self._set_disconnected_status()
            await asyncio.sleep(_RETRY_DELAY_SECONDS)

    # 校验服务端推送事件并统一分发到日志渲染逻辑。
    async def handle_event(self, message: dict[str, Any]) -> None:
        try:
            event = _EVENT_ADAPTER.validate_python(message)
        except ValidationError:
            return
        if isinstance(event, LlmTokenEvent):
            self._token_buffer += event.token
            self._token_run_id = event.run_id
            return
        self._flush_token_buffer()
        self.query_one("#event-log", RichLog).write(self._format_event(event))

    # 将缓冲的流式文本作为一条普通富文本日志一次性写入。
    def _flush_token_buffer(self) -> None:
        if not self._token_buffer:
            return
        run_id = self._token_run_id or "unknown"
        token_text = Text()
        token_text.append(f"[{run_id}] ", style="dim")
        token_text.append(self._token_buffer)
        self.query_one("#event-log", RichLog).write(token_text)
        self._token_buffer = ""
        self._token_run_id = None

    # 按事件类型构造带任务标识和语义配色的 Rich 文本行。
    def _format_event(self, event: AgentEvent) -> Text:
        text = Text()
        text.append(f"[{event.run_id}] ", style="dim")
        match event.type:
            case "run_started":
                text.append("RUN ", style="bold blue")
                text.append(event.goal)
            case "run_finished":
                self._append_run_finished(text, event)
            case "step_planning":
                text.append(f"STEP {event.step} ", style="bold cyan")
                text.append(event.plan)
            case "step_done":
                text.append(f"STEP {event.step} DONE ", style="dim")
                text.append(event.observation)
            case "tool_call_start":
                arguments = json.dumps(event.arguments, ensure_ascii=False, default=str)
                if len(arguments) > 160:
                    arguments = f"{arguments[:157]}..."
                text.append("CALL ", style="bold yellow")
                text.append(f"{event.tool_name} {arguments}")
            case "tool_call_success":
                text.append("TOOL ", style="bold green")
                text.append(f"{event.tool_name} ({event.duration_ms}ms)")
            case "tool_call_failed":
                text.append("TOOL FAILED ", style="bold red")
                text.append(f"{event.tool_name}: {event.error}")
            case "llm_request":
                text.append("LLM REQUEST", style="dim")
            case "llm_response":
                text.append(f"LLM RESPONSE ({event.duration_ms}ms)", style="dim")
        return text

    # 渲染成功或失败的任务完成信息。
    def _append_run_finished(self, text: Text, event: RunFinishedEvent) -> None:
        if event.status == "success":
            text.append("FINISHED ", style="bold green")
            text.append(f"{event.duration_ms}ms")
            return
        text.append("FAILED ", style="bold red")
        text.append(event.reason or "unknown error")

    # 更新状态栏为已连接的 daemon 地址。
    def _set_connected_status(self) -> None:
        self.query_one("#connection-status", Static).update(
            Text(f"connected {self._config.host}:{self._config.port}", style="green")
        )

    # 更新状态栏为断线后的自动重连提示。
    def _set_disconnected_status(self) -> None:
        self.query_one("#connection-status", Static).update(Text("disconnected - retrying...", style="yellow"))


# 加载配置并启动常驻的 Textual 观测客户端。
def main() -> None:
    try:
        config = load_config()
    except ConfigError as error:
        print(f"manius-tui: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    ManiusTui(config).run()
