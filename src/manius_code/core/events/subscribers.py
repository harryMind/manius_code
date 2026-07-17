import sys
from pathlib import Path
from typing import TextIO

from manius_code.core.events.models import AgentEvent


class StdoutPrinter:
    # 保存可替换的终端输出流，便于测试事件渲染。
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout

    # 将关键 Agent 事件渲染为终端中的单行状态日志。
    def handle(self, event: AgentEvent) -> None:
        if event.type == "llm_token":
            print(event.token, end="", file=self._stream, flush=True)
            return
        if event.type == "run_started":
            message = f"run {event.run_id} started: {event.goal}"
        elif event.type == "step_planning":
            message = f"step {event.step} planning: {event.plan}"
        elif event.type == "llm_response":
            message = ""
        elif event.type == "tool_call_start":
            message = f"step {event.step} tool start: {event.tool_name}"
        elif event.type == "tool_call_success":
            message = f"step {event.step} tool success: {event.tool_name} ({event.duration_ms}ms)"
        elif event.type == "tool_call_failed":
            message = f"step {event.step} tool failed: {event.tool_name}: {event.error}"
        elif event.type == "step_done":
            message = f"step {event.step} done: {event.observation}"
        elif event.type == "run_finished":
            message = f"run {event.run_id} {event.status}: {event.total_steps} steps, {event.duration_ms}ms"
        else:
            return
        print(message, file=self._stream, flush=True)


class EventWriter:
    # 打开指定的 JSONL 文件以持久化事件。
    def __init__(self, path: Path) -> None:
        self._file = path.open("a", encoding="utf-8")

    # 将单个事件追加为一行 JSON 并立即刷新。
    def handle(self, event: AgentEvent) -> None:
        self._file.write(event.model_dump_json() + "\n")
        self._file.flush()

    # 关闭事件文件句柄。
    def close(self) -> None:
        self._file.close()
