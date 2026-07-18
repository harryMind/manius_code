import sys
from pathlib import Path
from typing import TextIO

from manius_code.core.bus.events import AgentEvent


# 将关键 Agent 事件渲染为美观、分层、带颜色标识的终端实时日志
import sys
from typing import TextIO


class StdoutPrinter:
    def __init__(self, stream: TextIO | None = None):
        self._stream = stream or sys.stdout
        # ANSI 颜色码：按事件层级/状态区分视觉样式
        self._COLORS = {
            "run_started": "\033[94m",       # 亮蓝 - 任务启动
            "step_planning": "\033[96m",     # 亮青 - 步骤规划
            "tool_call_start": "\033[93m",   # 亮黄 - 工具调用中
            "tool_call_success": "\033[92m", # 亮绿 - 工具成功
            "tool_call_failed": "\033[91m",  # 亮红 - 工具失败
            "step_done": "\033[90m",         # 灰色 - 步骤收尾
            "run_success": "\033[92;1m",     # 亮绿加粗 - 任务成功
            "run_failed": "\033[91;1m",      # 亮红加粗 - 任务失败
            "reset": "\033[0m",
        }

    def handle(self, event: AgentEvent) -> None:
        reset = self._COLORS["reset"]

        # LLM 流式 token 逐字输出，不换行、无修饰
        if event.type == "llm_token":
            print(event.token, end="", file=self._stream, flush=True)
            return

        color = ""
        content = ""
        match event.type:
            case "run_started":
                color = self._COLORS["run_started"]
                content = f"▶ Run Started | ID: {event.run_id}\n  Goal: {event.goal}"

            case "step_planning":
                color = self._COLORS["step_planning"]
                content = f"◦ Step {event.step} | Planning: {event.plan}"

            case "tool_call_start":
                color = self._COLORS["tool_call_start"]
                # 参数超长自动截断，避免终端刷屏
                args_str = str(event.arguments)
                if len(args_str) > 40:
                    args_str = args_str[:37] + "..."
                content = f"⚙ Call Tool: {event.tool_name} | args: {args_str}"

            case "tool_call_success":
                color = self._COLORS["tool_call_success"]
                content = f"✅ Tool Success: {event.tool_name} | cost: {event.duration_ms}ms"

            case "tool_call_failed":
                color = self._COLORS["tool_call_failed"]
                content = f"❌ Tool Failed: {event.tool_name} | error: {event.error}"

            case "step_done":
                # 最终回答步骤跳过打印，避免与 LLM 流式输出重复
                if event.complete:
                    return
                color = self._COLORS["step_done"]
                obs = event.observation
                if len(obs) > 60:
                    obs = obs[:57] + "..."
                content = f"✓ Step {event.step} Done | {obs}"

            case "run_finished":
                if event.status == "success":
                    color = self._COLORS["run_success"]
                    content = f"\n■ Run Finished (Success) | Steps: {event.total_steps} | Cost: {event.duration_ms}ms"
                else:
                    color = self._COLORS["run_failed"]
                    reason = event.reason or "unknown error"
                    content = f"\n■ Run Finished (Failed) | Steps: {event.total_steps} | Cost: {event.duration_ms}ms\n  Reason: {reason}"
            case _:
                # 忽略 llm_request / llm_response 等内部事件，精简终端输出
                return

        print(f"{color}{content}{reset}", file=self._stream, flush=True)


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
