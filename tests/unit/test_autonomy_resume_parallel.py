import asyncio
import json
from pathlib import Path

from manius_code.core.agent.context import ExecutionContext
from manius_code.core.agent.runner import AgentRunner
from manius_code.core.autonomy.contracts import (
    AuditResult,
    AcceptanceCriterion,
    ActionProposal,
    Plan,
    PlanProposal,
    PlanStep,
    ResolverDecision,
    StepResult,
    VerificationResult,
)
from manius_code.core.autonomy.policy import AutonomyPolicy
from manius_code.core.autonomy.store import PlanStore
from manius_code.core.autonomy.supervisor import AutonomousSupervisor
from manius_code.core.config import ManiusConfig
from manius_code.core.events.bus import EventBus
from manius_code.core.tools.defaults import default_tool_catalog


class ResumableProvider:
    # 防止恢复流程意外重新生成新计划而掩盖 PlanStore.load 的恢复行为。
    async def plan(
        self,
        run_id: str,
        step: int,
        goal: str,
        memories: list[str],
        available_tools: list[str],
        audit_report: AuditResult | None = None,
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
        audit_report: AuditResult | None = None,
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


class RollingBatchProvider:
    # 保存每次批量动作请求中的步骤标识，以验证调度器按上限滚动绑定已就绪步骤。
    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    # 提供三个相互独立且分别拥有独立验收条件的原子读取步骤。
    async def plan(
        self,
        run_id: str,
        step: int,
        goal: str,
        memories: list[str],
        available_tools: list[str],
        audit_report: AuditResult | None = None,
    ) -> PlanProposal:
        return PlanProposal(
            goal=goal,
            steps=[
                PlanStep(
                    id=step_id,
                    title=step_id.title(),
                    allowed_tools=["read_file"],
                    acceptance_criteria=[AcceptanceCriterion(kind="tool_result_contains", expected=step_id)],
                )
                for step_id in ["first", "second", "third"]
            ],
        )

    # 阻止批量能力可用时意外回退到逐步骤动作规划接口。
    async def action(self, run_id: str, step: int, plan_step: PlanStep, history: list[StepResult]) -> ActionProposal:
        raise AssertionError("rolling batch execution must use the batch action interface")

    # 一次返回同一批每个原子步骤各自受限的读取动作。
    async def actions(
        self,
        run_id: str,
        step: int,
        plan_steps: list[PlanStep],
        history: list[StepResult],
    ) -> list[ActionProposal]:
        self.batches.append([plan_step.id for plan_step in plan_steps])
        return [
            ActionProposal(step_id=plan_step.id, tool_name="read_file", arguments={"path": "README.md"})
            for plan_step in plan_steps
        ]

    # 独立读取步骤应直接通过验收，不应进入故障修复路径。
    async def resolve(
        self,
        run_id: str,
        step: int,
        goal: str,
        plan_step: PlanStep,
        result: StepResult,
        history: list[StepResult],
    ) -> ResolverDecision:
        raise AssertionError("rolling batch steps should not need repair")

    # 返回所有批次完成后的稳定摘要以断言任务进入终态。
    async def summarize(self, run_id: str, step: int, goal: str, plan: PlanProposal, history: list[StepResult]) -> str:
        return "rolling batch completed"


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
            default_tool_catalog(ManiusConfig()),
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


# 功能：验证独立原子步骤会按滚动批次统一生成动作、审批并验收，而不是逐步骤走完整闭环。
# 设计：以三步骤和每批两个步骤构造边界，记录 Provider、Auditor 与 Verifier 的批量调用大小来覆盖动态绑定语义。
def test_supervisor_rolls_ready_steps_through_batched_approval_and_verification(tmp_path: Path) -> None:
    # 在单一事件循环中运行真实工具与批量替身，以验证分批动作请求和独立验收可共同成立。
    async def exercise() -> tuple[RollingBatchProvider, list[int], list[int], ExecutionContext]:
        (tmp_path / "README.md").write_text("first second third", encoding="utf-8")
        context = ExecutionContext(run_id="rolling-batch-run", goal="Read all sections")
        provider = RollingBatchProvider()
        supervisor = AutonomousSupervisor(
            context,
            provider,
            EventBus(),
            tmp_path / "run",
            tmp_path,
            AutonomyPolicy(max_steps=3, execution_batch_size=2),
            default_tool_catalog(ManiusConfig(workspace=tmp_path)),
        )
        audit_batch_sizes: list[int] = []
        verification_batch_sizes: list[int] = []
        original_approve_actions = supervisor._auditor.approve_actions
        original_verify_batch = supervisor._verifier.verify_batch

        # 记录一次集中动作审计覆盖的原子步骤数，同时复用真实审计实现。
        def approve_actions(steps: list[PlanStep], proposals: list[ActionProposal]) -> list[AuditResult]:
            audit_batch_sizes.append(len(steps))
            return original_approve_actions(steps, proposals)

        # 记录一次集中验收覆盖的交付物数量，同时复用真实验收规则。
        def verify_batch(verifications: list[tuple[PlanStep, StepResult]]) -> list[VerificationResult]:
            verification_batch_sizes.append(len(verifications))
            return original_verify_batch(verifications)

        supervisor._auditor.approve_actions = approve_actions
        supervisor._verifier.verify_batch = verify_batch
        await supervisor.run()
        return provider, audit_batch_sizes, verification_batch_sizes, context

    provider, audit_batch_sizes, verification_batch_sizes, context = asyncio.run(exercise())

    assert provider.batches == [["first", "second"], ["third"]]
    assert audit_batch_sizes == [2, 1]
    assert verification_batch_sizes == [2, 1]
    assert context.status == "success"
    assert context.step == 3
