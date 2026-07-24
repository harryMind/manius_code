import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from manius_code.core.agent.context import ExecutionContext
from manius_code.core.autonomy.contracts import Plan, StepResult
from manius_code.core.autonomy.planner import AutonomyProvider, StructuredAutonomyProvider
from manius_code.core.autonomy.policy import AutonomyPolicy
from manius_code.core.autonomy.store import PlanStore
from manius_code.core.autonomy.supervisor import AutonomousSupervisor
from manius_code.core.bus.events import RunFinishedEvent, RunResumedEvent, RunStartedEvent
from manius_code.core.config import ManiusConfig
from manius_code.core.events.bus import EventBus, Subscriber
from manius_code.core.events.subscribers import EventWriter
from manius_code.core.llm.anthropic import AnthropicProvider
from manius_code.core.tracing import TracingProvider
from manius_code.core.tools.catalog import ToolCatalog
from manius_code.core.tools.defaults import default_tool_catalog


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
        tool_factory: Callable[[ManiusConfig], ToolCatalog] = default_tool_catalog,
        event_subscribers: list[Subscriber] | None = None,
        tracer: TracingProvider | None = None,
    ) -> None:
        self._config = config
        self._runs_dir = runs_dir
        self._provider_factory = provider_factory
        self._tool_factory = tool_factory
        self._event_subscribers = event_subscribers or []
        self._tracer = tracer

    # 为新任务创建运行目录并执行五层自主规划闭环。
    async def run(self, goal: str, run_id: str | None = None) -> RunSummary:
        run_id = run_id or uuid4().hex
        run_dir = self._runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        return await self._execute(run_id, run_dir, goal)

    # 校验指定运行是否具备恢复条件，供 RPC 在创建后台任务之前返回明确错误。
    def validate_resume(self, run_id: str) -> None:
        self._load_resumable(run_id)

    # 从不可变计划版本和最新状态快照恢复尚未结束的运行。
    async def resume(self, run_id: str) -> RunSummary:
        run_dir, plan, history, previous_step = self._load_resumable(run_id)
        return await self._execute(
            run_id,
            run_dir,
            plan.goal,
            plan=plan,
            history=history,
            previous_step=previous_step,
        )

    # 组装单次新建或恢复运行的事件、上下文和五层闭环，并统一持久化终态事件。
    async def _execute(
        self,
        run_id: str,
        run_dir: Path,
        goal: str,
        plan: Plan | None = None,
        history: list[StepResult] | None = None,
        previous_step: int = 0,
    ) -> RunSummary:
        event_bus = EventBus(self._tracer)
        writer = EventWriter(run_dir / "events.jsonl")
        event_bus.subscribe(writer.handle)
        for subscriber in self._event_subscribers:
            event_bus.subscribe(subscriber)

        context = ExecutionContext(run_id=run_id, goal=goal, step=previous_step)
        if plan is None:
            context.initialize()
        tools = self._tool_factory(self._config)
        provider = self._make_provider(event_bus, tools)
        supervisor = AutonomousSupervisor(
            context,
            provider,
            event_bus,
            run_dir,
            self._config.workspace,
            AutonomyPolicy(
                max_steps=self._config.max_steps,
                execution_batch_size=self._config.execution_batch_size,
            ),
            tools,
        )

        started_at = time.monotonic()
        try:
            if plan is None:
                await event_bus.publish(RunStartedEvent(run_id=run_id, goal=goal, run_dir=str(run_dir)))
            else:
                await event_bus.publish(
                    RunResumedEvent(
                        run_id=run_id,
                        step=previous_step,
                        goal=goal,
                        run_dir=str(run_dir),
                        previous_step=previous_step,
                    )
                )
            await supervisor.run(plan=plan, history=history)
            if context.status == "running":
                context.mark_failed("supervisor returned without a terminal task status")
        except asyncio.CancelledError:
            writer.close()
            raise
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
        try:
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
        finally:
            writer.close()
        return summary

    # 读取可恢复状态、既有尝试历史和历史步骤编号，并规范化中断时残留的运行态。
    def _load_resumable(self, run_id: str) -> tuple[Path, Plan, list[StepResult], int]:
        run_dir = self._runs_dir / run_id
        if not run_dir.is_dir():
            raise FileNotFoundError(f"run not found: {run_id}")
        if self._has_finished_event(run_dir):
            raise ValueError(f"run has already finished: {run_id}")
        plans = PlanStore(run_dir)
        return run_dir, plans.load_resumable(), plans.load_attempts(), self._last_event_step(run_dir)

    # 判断事件流是否已经写入任务终态，避免用户对同一已完成运行重复执行恢复。
    def _has_finished_event(self, run_dir: Path) -> bool:
        event_path = run_dir / "events.jsonl"
        if not event_path.is_file():
            return False
        for line in event_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "run_finished":
                return True
        return False

    # 从历史事件中取得最大步骤编号，使恢复后的事件编号保持单调递增。
    def _last_event_step(self, run_dir: Path) -> int:
        event_path = run_dir / "events.jsonl"
        if not event_path.is_file():
            return 0
        highest_step = 0
        for line in event_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            step = event.get("step")
            if isinstance(step, int):
                highest_step = max(highest_step, step)
        return highest_step

    # 按注入的测试替身或默认 Anthropic 适配器构造结构化自主规划 Provider。
    def _make_provider(self, event_bus: EventBus, tools: ToolCatalog) -> AutonomyProvider:
        if self._provider_factory is not None:
            return self._provider_factory(event_bus)
        return StructuredAutonomyProvider(
            AnthropicProvider(self._config.llm, event_bus, [], tracer=self._tracer),
            tools.argument_models(),
            workspace=self._config.workspace,
        )
