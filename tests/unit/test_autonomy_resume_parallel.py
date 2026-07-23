import asyncio
import json
from pathlib import Path

from manius_code.core.agent.context import ExecutionContext
from manius_code.core.agent.runner import AgentRunner
from manius_code.core.autonomy.contracts import AcceptanceCriterion, ActionProposal, Plan, PlanProposal, PlanStep, ResolverDecision, StepResult
from manius_code.core.autonomy.policy import AutonomyPolicy
from manius_code.core.autonomy.store import PlanStore
from manius_code.core.autonomy.supervisor import AutonomousSupervisor
from manius_code.core.config import ManiusConfig
from manius_code.core.events.bus import EventBus


class ResumableProvider:
    # 防止恢复流程意外重新生成新计划而掩盖 PlanStore.load 的恢复行为。
    async def plan(
        self,
        run_id: str,
        step: int,
        goal: str,
        memories: list[str],
        available_tools: list[str],
    ) -> PlanProposal:
        raise AssertionError("resume must reuse the persisted plan")

    # 为恢复后的既有步骤生成与验收条件匹配的只读动作。
    async def action(self, run_id: str, step: int, plan_step: PlanStep, history: list[StepResult]) -> ActionProposal:
        return ActionProposal(step_id=plan_step.id, tool_name="read_file", arguments={"path": "README.md"})

    # 使本测试在出现意外工具失败时立即暴露，而不是静默进入修复分支。
    async def resolve(
        self,
        run_id: str,
        step: int,
        goal: str,
        plan_step: PlanStep,
        result: StepResult,
        history: list[StepResult],
    ) -> ResolverDecision:
        raise AssertionError("the resumed read should pass without repair")

    # 返回恢复计划全部验收完成后的稳定结果。
    async def summarize(self, run_id: str, step: int, goal: str, plan: PlanProposal, history: list[StepResult]) -> str:
        return "resumed plan completed"


class ParallelProvider:
    # 生成两个互不依赖且可在同一调度批次中执行的步骤。
    async def plan(
        self,
        run_id: str,
        step: int,
        goal: str,
        memories: list[str],
        available_tools: list[str],
    ) -> PlanProposal:
        return PlanProposal(
            goal=goal,
            steps=[
                PlanStep(
                    id="first",
                    title="First",
                    allowed_tools=["read_file"],
                    acceptance_criteria=[AcceptanceCriterion(kind="tool_result_contains", expected="first")],
                ),
                PlanStep(
                    id="second",
                    title="Second",
                    allowed_tools=["read_file"],
                    acceptance_criteria=[AcceptanceCriterion(kind="tool_result_contains", expected="second")],
                ),
            ],
        )

    # 为每个并发步骤产生相同类型但携带各自标识的可审计动作。
    async def action(self, run_id: str, step: int, plan_step: PlanStep, history: list[StepResult]) -> ActionProposal:
        return ActionProposal(step_id=plan_step.id, tool_name="read_file", arguments={"path": "README.md"})

    # 防止并发成功路径意外进入修复器而降低测试的定位能力。
    async def resolve(
        self,
        run_id: str,
        step: int,
        goal: str,
        plan_step: PlanStep,
        result: StepResult,
        history: list[StepResult],
    ) -> ResolverDecision:
        raise AssertionError("parallel steps should not need repair")

    # 汇总两个并行步骤的已验证观察结果。
    async def summarize(self, run_id: str, step: int, goal: str, plan: PlanProposal, history: list[StepResult]) -> str:
        return "parallel plan completed"


# 功能：验证 AgentRunner 会从 state.json 和不可变计划恢复中断步骤，并持续写入同一事件流。
# 设计：预先持久化 pending 步骤且让 Provider.plan 直接失败，确保测试只能通过 PlanStore.load 恢复而非重新规划。
def test_agent_runner_resumes_persisted_plan(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "README.md").write_text("resume evidence", encoding="utf-8")
    run_id = "resume-run"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    plan = Plan(
        version=1,
        goal="Resume the saved plan",
        steps=[
            PlanStep(
                id="readme",
                title="Read README",
                allowed_tools=["read_file"],
                acceptance_criteria=[AcceptanceCriterion(kind="tool_result_contains", expected="resume evidence")],
            )
        ],
    )
    PlanStore(run_dir).persist(plan)
    runner = AgentRunner(
        ManiusConfig(max_steps=3),
        runs_dir=tmp_path / "runs",
        provider_factory=lambda _bus: ResumableProvider(),
    )

    summary = asyncio.run(runner.resume(run_id))
    event_types = [json.loads(line)["type"] for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]

    assert summary.status == "success"
    assert summary.result == "resumed plan completed"
    assert event_types[0] == "run_resumed"
    assert event_types[-1] == "run_finished"
    assert PlanStore(run_dir).load().steps[0].status == "succeeded"


# 功能：验证步骤已完成但尚未写入 run_finished 时，恢复会补齐汇总而不是被误判为已完成任务。
# 设计：预置 succeeded 快照且不创建事件文件，隔离“计划完成”与“任务终态事件已写入”这两个恢复条件。
def test_agent_runner_resumes_completed_plan_without_finished_event(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    run_id = "summary-run"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    plan = Plan(
        version=1,
        goal="Summarize the saved plan",
        steps=[PlanStep(id="done", title="Done", status="succeeded")],
    )
    PlanStore(run_dir).persist(plan)
    runner = AgentRunner(
        ManiusConfig(max_steps=3),
        runs_dir=tmp_path / "runs",
        provider_factory=lambda _bus: ResumableProvider(),
    )

    summary = asyncio.run(runner.resume(run_id))

    assert summary.status == "success"
    assert summary.result == "resumed plan completed"
    assert [json.loads(line)["type"] for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()] == [
        "run_resumed",
        "run_finished",
    ]


# 功能：验证 Scheduler.ready_steps 返回的同批独立步骤会被 Supervisor 原生并发执行且保留唯一步骤编号。
# 设计：以可控异步 Executor 替身制造执行重叠，并在真实审计和验收路径中断言峰值并发数与上下文步骤计数。
def test_supervisor_executes_ready_steps_in_parallel(tmp_path: Path) -> None:
    # 在一个事件循环内组装 Supervisor 并注入可观测的并发执行替身。
    async def exercise() -> tuple[int, ExecutionContext]:
        context = ExecutionContext(run_id="parallel-run", goal="Run independent steps")
        supervisor = AutonomousSupervisor(
            context,
            ParallelProvider(),
            EventBus(),
            tmp_path / "run",
            tmp_path,
            AutonomyPolicy(max_steps=3),
        )
        in_flight = 0
        maximum_in_flight = 0

        # 通过短暂让出事件循环证明两个 Executor 调用确实在同一批次重叠。
        async def execute(proposal: ActionProposal, step: int, attempt: int) -> StepResult:
            nonlocal in_flight, maximum_in_flight
            in_flight += 1
            maximum_in_flight = max(maximum_in_flight, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return StepResult(
                step_id=proposal.step_id,
                attempt=attempt,
                tool_name=proposal.tool_name,
                observation=proposal.step_id,
            )

        supervisor._executor.execute = execute
        await supervisor.run()
        return maximum_in_flight, context

    maximum_in_flight, context = asyncio.run(exercise())

    assert maximum_in_flight == 2
    assert context.status == "success"
    assert context.step == 2
