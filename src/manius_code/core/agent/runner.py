import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel

from manius_code.core.agent.context import ExecutionContext
from manius_code.core.agent.loop import AgentLoop
from manius_code.core.config import ManiusConfig
from manius_code.core.events.bus import EventBus, Subscriber
from manius_code.core.bus.events import RunFinishedEvent, RunStartedEvent
from manius_code.core.events.subscribers import EventWriter
from manius_code.core.llm.anthropic import AnthropicProvider
from manius_code.core.tools.invocation import ToolInvoker
from manius_code.core.tools.read_file import ReadFileTool
from manius_code.core.tools.registry import ToolRegistry


class RunSummary(BaseModel):
    run_id: str
    status: Literal["success", "failed"]
    total_steps: int
    duration_ms: int
    result: str = ""
    reason: str | None = None


class AgentRunner:
    # 注入应用配置、运行目录和可选的 Claude Provider 工厂。
    def __init__(
        self,
        config: ManiusConfig,
        runs_dir: Path = Path("runs"),
        provider_factory: Callable[[EventBus, list[dict[str, Any]]], AnthropicProvider] | None = None,
        event_subscribers: list[Subscriber] | None = None,
    ) -> None:
        self._config = config
        self._runs_dir = runs_dir
        self._provider_factory = provider_factory
        self._event_subscribers = event_subscribers or []

    # 创建一次运行的依赖并返回其最终汇总信息。
    async def run(self, goal: str, run_id: str | None = None) -> RunSummary:
        # 任务ID
        run_id = run_id or uuid4().hex
        run_dir = self._runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)

        # 全局事件总线
        event_bus = EventBus()
        writer = EventWriter(run_dir / "events.jsonl")

        # 其实daemon端不用注册终端输出事件，因为在事件推送中会回调hand_event函数执行printer.handle
        # if self._print_events:
        #     event_bus.subscribe(StdoutPrinter().handle)
        event_bus.subscribe(writer.handle)
        for subscriber in self._event_subscribers:
            event_bus.subscribe(subscriber)

        # 全局状态容器
        context = ExecutionContext(run_id=run_id, goal=goal)
        context.initialize()

        # 注册工具
        tools = ToolRegistry()
        # s1 仅读取文件工具
        tools.register(ReadFileTool())
        tool_invoker = ToolInvoker(tools, event_bus, run_id, lambda: context.step)

        # llm绑定事件总线与工具注册表
        provider = self._make_provider(event_bus, tools)

        # 智能体核心主循环（Plan → Act → Observe）
        loop = AgentLoop(context, provider, tool_invoker, event_bus, self._config.max_steps)
        started_at = time.monotonic()
        await event_bus.publish(RunStartedEvent(run_id=run_id, goal=goal, run_dir=str(run_dir)))
        try:
            await loop.run()
        except Exception as error:
            if context.status == "running":
                context.mark_failed(str(error))
            duration_ms = round((time.monotonic() - started_at) * 1000)
            summary = RunSummary(
                run_id=run_id,
                status=context.status,
                total_steps=context.step,
                duration_ms=duration_ms,
                result=context.result,
                reason=context.reason,
            )
            await event_bus.publish(
                RunFinishedEvent(
                    run_id=run_id,
                    step=context.step,
                    status=context.status,
                    total_steps=context.step,
                    duration_ms=duration_ms,
                    summary=context.reason or "",
                    reason=context.reason,
                )
            )
        else:
            duration_ms = round((time.monotonic() - started_at) * 1000)
            summary = RunSummary(
                run_id=run_id,
                status=context.status,
                total_steps=context.step,
                duration_ms=duration_ms,
                result=context.result,
                reason=context.reason,
            )
            await event_bus.publish(
                RunFinishedEvent(
                    run_id=run_id,
                    step=context.step,
                    status=context.status,
                    total_steps=context.step,
                    duration_ms=duration_ms,
                    summary=context.result,
                    reason=context.reason,
                )
            )
        finally:
            writer.close()
        return summary

    # 按默认 Anthropic 配置或测试工厂构造 Provider。
    def _make_provider(self, event_bus: EventBus, tools: ToolRegistry) -> AnthropicProvider:
        if self._provider_factory:
            return self._provider_factory(event_bus, tools.definitions())
        return AnthropicProvider(self._config.llm, event_bus, tools.definitions())
