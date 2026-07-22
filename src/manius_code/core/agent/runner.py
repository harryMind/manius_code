import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from manius_code.core.agent.context import ExecutionContext
from manius_code.core.autonomy.planner import AutonomyProvider, StructuredAutonomyProvider
from manius_code.core.autonomy.policy import AutonomyPolicy
from manius_code.core.autonomy.supervisor import AutonomousSupervisor
from manius_code.core.bus.events import RunFinishedEvent, RunStartedEvent
from manius_code.core.config import ManiusConfig
from manius_code.core.events.bus import EventBus, Subscriber
from manius_code.core.events.subscribers import EventWriter
from manius_code.core.llm.anthropic import AnthropicProvider
from manius_code.core.tracing import TracingProvider


class RunSummary(BaseModel):
    run_id: str
    status: Literal["success", "failed"]
    total_steps: int
    duration_ms: int
    result: str = ""
    reason: str | None = None


class AgentRunner:
    # 注入应用配置、运行目录和满足五层闭环契约的可选模型工厂。
    def __init__(
        self,
        config: ManiusConfig,
        runs_dir: Path = Path("runs"),
        provider_factory: Callable[[EventBus], AutonomyProvider] | None = None,
        event_subscribers: list[Subscriber] | None = None,
        tracer: TracingProvider | None = None,
    ) -> None:
        self._config = config
        self._runs_dir = runs_dir
        self._provider_factory = provider_factory
        self._event_subscribers = event_subscribers or []
        self._tracer = tracer

    # 为一次任务构建五层闭环并持久化其唯一的开始、过程和完成事件。
    async def run(self, goal: str, run_id: str | None = None) -> RunSummary:
        run_id = run_id or uuid4().hex
        run_dir = self._runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        event_bus = EventBus(self._tracer)
        writer = EventWriter(run_dir / "events.jsonl")
        event_bus.subscribe(writer.handle)
        for subscriber in self._event_subscribers:
            event_bus.subscribe(subscriber)

        context = ExecutionContext(run_id=run_id, goal=goal)
        context.initialize()
        provider = self._make_provider(event_bus)
        supervisor = AutonomousSupervisor(
            context,
            provider,
            event_bus,
            run_dir,
            Path.cwd(),
            AutonomyPolicy(max_steps=self._config.max_steps),
        )

        # 旧 S4 的 TaskManager、AgentLoop、ToolRegistry 和 ToolInvoker 路径保留为兼容代码，新的运行入口不注册也不调用它们。
        started_at = time.monotonic()
        await event_bus.publish(RunStartedEvent(run_id=run_id, goal=goal, run_dir=str(run_dir)))
        try:
            await supervisor.run()
        except Exception as error:
            if context.status == "running":
                context.mark_failed(str(error))
        finally:
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
                    summary=context.result if context.status == "success" else context.reason or "",
                    reason=context.reason,
                )
            )
            writer.close()
        return summary

    # 按注入的测试替身或默认 Anthropic 适配器构造结构化自主规划 Provider。
    def _make_provider(self, event_bus: EventBus) -> AutonomyProvider:
        if self._provider_factory is not None:
            return self._provider_factory(event_bus)
        return StructuredAutonomyProvider(AnthropicProvider(self._config.llm, event_bus, [], tracer=self._tracer))
