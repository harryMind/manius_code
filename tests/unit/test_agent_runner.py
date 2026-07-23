import asyncio
import json
from pathlib import Path

import pytest

from manius_code.core.agent.runner import AgentRunner
from manius_code.core.autonomy.auditor import Auditor
from manius_code.core.autonomy.contracts import (
    AcceptanceCriterion,
    ActionProposal,
    PlanProposal,
    PlanStep,
    ResolverDecision,
    StepResult,
)
from manius_code.core.config import ManiusConfig
from manius_code.core.events.bus import EventBus


class ReadmeProvider:
    # 保存 Supervisor 注入的动态工具白名单以便断言 Planner 不依赖旧注册表。
    def __init__(self) -> None:
        self.available_tools: list[str] = []

    # 提供只包含一个可验证读取步骤的稳定计划。
    async def plan(
        self,
        run_id: str,
        step: int,
        goal: str,
        memories: list[str],
        available_tools: list[str],
    ) -> PlanProposal:
        self.available_tools = available_tools
        return PlanProposal(
            goal=goal,
            steps=[
                PlanStep(
                    id="readme",
                    title="Read README",
                    allowed_tools=["read_file"],
                    acceptance_criteria=[AcceptanceCriterion(kind="tool_result_contains", expected="Main sections")],
                )
            ],
        )

    # 为读取步骤返回唯一受审计的相对路径动作。
    async def action(self, run_id: str, step: int, plan_step: PlanStep, history: list[StepResult]) -> ActionProposal:
        return ActionProposal(step_id=plan_step.id, tool_name="read_file", arguments={"path": "README.md"})

    # 此稳定计划不应触发修复决策。
    async def resolve(
        self,
        run_id: str,
        step: int,
        goal: str,
        plan_step: PlanStep,
        result: StepResult,
        history: list[StepResult],
    ) -> ResolverDecision:
        raise AssertionError("README plan should not need repair")

    # 为全部验证完成的读取任务返回最终用户摘要。
    async def summarize(self, run_id: str, step: int, goal: str, plan: PlanProposal, history: list[StepResult]) -> str:
        return "README analysis completed."


class RetryingWriteProvider:
    # 初始化动作计数以让首个真实工具失败后进入重试路径。
    def __init__(self) -> None:
        self._attempts = 0

    # 提供一个写入并验证文件内容的交付计划。
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
                    id="write",
                    title="Write result",
                    allowed_tools=["write_file"],
                    acceptance_criteria=[
                        AcceptanceCriterion(kind="file_contains", path="result.py", expected="return 1")
                    ],
                )
            ],
        )

    # 先写向目录触发真实工具错误，再写入通过验收的目标文件。
    async def action(self, run_id: str, step: int, plan_step: PlanStep, history: list[StepResult]) -> ActionProposal:
        self._attempts += 1
        path = "." if self._attempts == 1 else "result.py"
        return ActionProposal(
            step_id=plan_step.id,
            tool_name="write_file",
            arguments={"path": path, "content": "def answer() -> int:\n    return 1\n"},
        )

    # 针对首次工具失败要求调度器重试同一已审计步骤。
    async def resolve(
        self,
        run_id: str,
        step: int,
        goal: str,
        plan_step: PlanStep,
        result: StepResult,
        history: list[StepResult],
    ) -> ResolverDecision:
        return ResolverDecision(action="retry", reason="use the declared output file instead of a directory")

    # 返回已验证文件交付的简短总结。
    async def summarize(self, run_id: str, step: int, goal: str, plan: PlanProposal, history: list[StepResult]) -> str:
        return "result.py created and verified."


class ReplanningProvider:
    # 初始化标志以便只在第一次失败后提交替换计划。
    def __init__(self) -> None:
        self._replanned = False

    # 先提供会失败的读取计划，借此验证动态重规划闭环。
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
                    id="missing",
                    title="Inspect unavailable source",
                    allowed_tools=["read_file"],
                    acceptance_criteria=[AcceptanceCriterion(kind="tool_result_contains", expected="source")],
                )
            ],
        )

    # 根据当前计划步骤选择失败读取或替代计划中的安全写入。
    async def action(self, run_id: str, step: int, plan_step: PlanStep, history: list[StepResult]) -> ActionProposal:
        if plan_step.id == "missing":
            return ActionProposal(step_id="missing", tool_name="read_file", arguments={"path": "missing.txt"})
        return ActionProposal(
            step_id="deliver",
            tool_name="write_file",
            arguments={"path": "replanned.txt", "content": "replanned\n"},
        )

    # 第一次失败提交完整替换计划，以验证计划版本切换后能够继续调度。
    async def resolve(
        self,
        run_id: str,
        step: int,
        goal: str,
        plan_step: PlanStep,
        result: StepResult,
        history: list[StepResult],
    ) -> ResolverDecision:
        self._replanned = True
        return ResolverDecision(
            action="replan",
            reason="the source does not exist; deliver a verified replacement artifact",
            plan=PlanProposal(
                goal=goal,
                steps=[
                    PlanStep(
                        id="deliver",
                        title="Deliver replacement",
                        allowed_tools=["write_file"],
                        acceptance_criteria=[AcceptanceCriterion(kind="file_exists", path="replanned.txt")],
                    )
                ],
            ),
        )

    # 返回替换计划产生的验证结果。
    async def summarize(self, run_id: str, step: int, goal: str, plan: PlanProposal, history: list[StepResult]) -> str:
        return "Replacement artifact delivered."


class RevisingCriteriaProvider:
    # 提供一个会在首次验收失败后仅修订验收条件的目录检查计划。
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
                    id="inspect",
                    title="Inspect workspace",
                    allowed_tools=["list_dir"],
                    acceptance_criteria=[AcceptanceCriterion(kind="tool_result_contains", expected="not-present")],
                )
            ],
        )

    # 为目录检查步骤返回唯一的只读工具动作。
    async def action(self, run_id: str, step: int, plan_step: PlanStep, history: list[StepResult]) -> ActionProposal:
        return ActionProposal(step_id=plan_step.id, tool_name="list_dir", arguments={"path": ".", "max_depth": 0})

    # 用同一步骤标识修订错误的验收字符串，使现有工具输出可被直接重新验证。
    async def resolve(
        self,
        run_id: str,
        step: int,
        goal: str,
        plan_step: PlanStep,
        result: StepResult,
        history: list[StepResult],
    ) -> ResolverDecision:
        return ResolverDecision(
            action="revise_step",
            reason="the directory listing already proves the expected file exists",
            revised_step=plan_step.model_copy(
                update={"acceptance_criteria": [AcceptanceCriterion(kind="tool_result_contains", expected="README.md")]}
            ),
        )

    # 返回通过既有观察结果验证后的稳定摘要。
    async def summarize(self, run_id: str, step: int, goal: str, plan: PlanProposal, history: list[StepResult]) -> str:
        return "Existing observation verified after criteria revision."


class InvalidPlanProvider:
    # 生成含循环依赖的非法 DAG 以验证审计层不会让它进入执行层。
    async def plan(
        self,
        run_id: str,
        step: int,
        goal: str,
        memories: list[str],
        available_tools: list[str],
    ) -> PlanProposal:
        criterion = AcceptanceCriterion(kind="tool_result_contains", expected="unused")
        return PlanProposal(
            goal=goal,
            steps=[
                PlanStep(id="first", title="First", dependencies=["second"], allowed_tools=["read_file"], acceptance_criteria=[criterion]),
                PlanStep(id="second", title="Second", dependencies=["first"], allowed_tools=["read_file"], acceptance_criteria=[criterion]),
            ],
        )

    # 非法计划必须在动作规划前被审计层拒绝。
    async def action(self, run_id: str, step: int, plan_step: PlanStep, history: list[StepResult]) -> ActionProposal:
        raise AssertionError("invalid plan must not reach the Executor")

    # 非法计划必须在修复器介入前被审计层拒绝。
    async def resolve(
        self,
        run_id: str,
        step: int,
        goal: str,
        plan_step: PlanStep,
        result: StepResult,
        history: list[StepResult],
    ) -> ResolverDecision:
        raise AssertionError("invalid plan must not reach the Resolver")

    # 非法计划不可能被汇总为成功结果。
    async def summarize(self, run_id: str, step: int, goal: str, plan: PlanProposal, history: list[StepResult]) -> str:
        raise AssertionError("invalid plan must not be summarized")


class EscapedGoalProvider(ReadmeProvider):
    # 模拟模型省略礼貌前缀并将 Windows 路径反斜杠重复转义的计划回显。
    async def plan(
        self,
        run_id: str,
        step: int,
        goal: str,
        memories: list[str],
        available_tools: list[str],
    ) -> PlanProposal:
        proposal = await super().plan(run_id, step, goal, memories, available_tools)
        return proposal.model_copy(update={"goal": goal.removeprefix("请").replace("\\", "\\\\")})


# 功能：验证运行器以五层闭环执行受审计计划、持久化计划状态和验证事件，而非旧的直接工具调用循环。
# 设计：注入结构化替身并读取真实 events.jsonl，覆盖规划、执行、验收、记忆和外部可观测性的完整边界。
def test_agent_runner_executes_verified_plan_and_persists_memory(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "README.md").write_text("# Main sections\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    provider = ReadmeProvider()
    runner = AgentRunner(
        ManiusConfig(max_steps=3),
        runs_dir=tmp_path / "runs",
        provider_factory=lambda _bus: provider,
    )

    summary = asyncio.run(runner.run("Analyze README.md"))
    run_dir = tmp_path / "runs" / summary.run_id
    event_types = [json.loads(line)["type"] for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    memory = json.loads((tmp_path / ".manius" / "memory" / "episodes.jsonl").read_text(encoding="utf-8"))

    assert summary.status == "success"
    assert summary.result == "README analysis completed."
    assert (run_dir / "plan" / "plan.v1.json").is_file()
    assert json.loads((run_dir / "plan" / "state.json").read_text(encoding="utf-8"))["steps"][0]["status"] == "succeeded"
    assert not (run_dir / ".tasks").exists()
    assert (tmp_path / ".manius" / "memory" / "episodes.jsonl").is_file()
    assert memory["tool_preferences"] == ["read_file"]
    assert memory["verified_steps"][0]["id"] == "readme"
    assert provider.available_tools == ["bash", "list_dir", "read_file", "write_file"]
    assert {"plan_proposed", "plan_approved", "step_verified", "tool_call_success"}.issubset(event_types)
    assert event_types[-1] == "run_finished"


# 功能：验证模型回显目标时改变 Windows 路径转义或礼貌前缀不会使有效计划在第零步失败。
# 设计：使用只改写 PlanProposal.goal 的替身保留真实读取、验收与持久化流程，断言持久化计划仍采用外部请求的原始目标。
def test_agent_runner_uses_requested_goal_when_model_echo_is_escaped(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "README.md").write_text("# Main sections\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    goal = r"请在D:\workspace\code\test中分析README.md"
    runner = AgentRunner(
        ManiusConfig(max_steps=3),
        runs_dir=tmp_path / "runs",
        provider_factory=lambda _bus: EscapedGoalProvider(),
    )

    summary = asyncio.run(runner.run(goal))
    persisted = json.loads((tmp_path / "runs" / summary.run_id / "plan" / "plan.v1.json").read_text(encoding="utf-8"))

    assert summary.status == "success"
    assert persisted["goal"] == goal


# 功能：验证审计器允许配置工作区内的绝对路径，同时继续拒绝工作区外路径。
# 设计：直接构造同一计划的验收路径与动作路径，避免依赖模型输出格式并分别覆盖允许和拒绝边界。
def test_auditor_allows_absolute_paths_only_inside_configured_workspace(tmp_path: Path) -> None:
    inside = tmp_path / "result.py"
    proposal = PlanProposal(
        goal="Write result",
        steps=[
            PlanStep(
                id="write",
                title="Write result",
                allowed_tools=["write_file"],
                acceptance_criteria=[AcceptanceCriterion(kind="file_exists", path=str(inside))],
                artifacts=[str(inside)],
            )
        ],
    )
    auditor = Auditor({"write_file"}, tmp_path)

    auditor.approve_plan(proposal)
    auditor.approve_action(
        proposal.steps[0],
        ActionProposal(step_id="write", tool_name="write_file", arguments={"path": str(inside), "content": "ok"}),
    )
    outside = tmp_path.parent / "outside.py"
    proposal.steps[0].acceptance_criteria[0].path = str(outside)
    proposal.steps[0].artifacts = [str(outside)]
    with pytest.raises(ValueError, match="unsafe acceptance path"):
        auditor.approve_plan(proposal)


# 功能：验证真实工具错误不会结束任务，而是经 Resolver 返回调度器并在下一次尝试后完成验收。
# 设计：首次选择目录触发 WriteFileTool 错误，断言事件和 attempts 记录均保留失败事实，避免仅依赖替身行为。
def test_agent_runner_recovers_from_tool_failure_with_a_verified_retry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = AgentRunner(
        ManiusConfig(max_steps=3),
        runs_dir=tmp_path / "runs",
        provider_factory=lambda _bus: RetryingWriteProvider(),
    )

    summary = asyncio.run(runner.run("Write result.py"))
    run_dir = tmp_path / "runs" / summary.run_id
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    attempts = [json.loads(line) for line in (run_dir / "plan" / "attempts.jsonl").read_text(encoding="utf-8").splitlines()]

    assert summary.status == "success"
    assert summary.total_steps == 2
    assert (tmp_path / "result.py").read_text(encoding="utf-8").endswith("return 1\n")
    assert any(event["type"] == "tool_call_failed" for event in events)
    assert attempts[0]["error"] is not None
    assert attempts[-1]["error"] is None


# 功能：验证运行器会将配置工作区同时注入文件工具、验收器和任务记忆。
# 设计：让 daemon 启动目录与任务根目录分离，并复用真实写入重试 Provider 覆盖执行与验收路径的一致性。
def test_agent_runner_uses_configured_workspace_for_external_task_output(tmp_path: Path, monkeypatch) -> None:
    launcher = tmp_path / "daemon"
    workspace = tmp_path / "student-system"
    launcher.mkdir()
    workspace.mkdir()
    monkeypatch.chdir(launcher)
    runner = AgentRunner(
        ManiusConfig(max_steps=3, workspace=workspace),
        runs_dir=tmp_path / "runs",
        provider_factory=lambda _bus: RetryingWriteProvider(),
    )

    summary = asyncio.run(runner.run("Write result.py in the configured workspace"))

    assert summary.status == "success"
    assert (workspace / "result.py").read_text(encoding="utf-8").endswith("return 1\n")
    assert (workspace / ".manius" / "memory" / "episodes.jsonl").is_file()
    assert not (launcher / "result.py").exists()


# 功能：验证 Resolver 的 replan 决策会替换活动计划并继续由调度器执行新版本，而不是中止任务。
# 设计：先执行必然失败的读取动作，再检查两份计划版本、修订事件和最终产物，覆盖版本切换的状态闭环。
def test_agent_runner_replaces_plan_after_resolver_replan(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    provider = ReplanningProvider()
    runner = AgentRunner(
        ManiusConfig(max_steps=4),
        runs_dir=tmp_path / "runs",
        provider_factory=lambda _bus: provider,
    )

    summary = asyncio.run(runner.run("Create a replacement artifact"))
    plan_dir = tmp_path / "runs" / summary.run_id / "plan"
    event_types = [json.loads(line)["type"] for line in (tmp_path / "runs" / summary.run_id / "events.jsonl").read_text(encoding="utf-8").splitlines()]

    assert summary.status == "success"
    assert provider._replanned is True
    assert (tmp_path / "replanned.txt").read_text(encoding="utf-8") == "replanned\n"
    assert (plan_dir / "plan.v1.json").is_file()
    assert (plan_dir / "plan.v2.json").is_file()
    assert "plan_revised" in event_types


# 功能：验证 revise_step 会持久化修订计划并使用已有成功工具结果重新验收，不重复执行同一个工具动作。
# 设计：先构造必然不匹配的文本验收条件，再由替身仅修订条件，断言执行步数与 tool_call_start 均为一次。
def test_agent_runner_reuses_observation_after_revising_acceptance_criteria(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "README.md").write_text("# Read me\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runner = AgentRunner(
        ManiusConfig(max_steps=2),
        runs_dir=tmp_path / "runs",
        provider_factory=lambda _bus: RevisingCriteriaProvider(),
    )

    summary = asyncio.run(runner.run("Inspect README"))
    run_dir = tmp_path / "runs" / summary.run_id
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    state = json.loads((run_dir / "plan" / "state.json").read_text(encoding="utf-8"))

    assert summary.status == "success"
    assert summary.total_steps == 1
    assert (run_dir / "plan" / "plan.v2.json").is_file()
    assert state["version"] == 2
    assert state["steps"][0]["status"] == "succeeded"
    assert [event["type"] for event in events].count("tool_call_start") == 1
    assert "plan_revised" in [event["type"] for event in events]


# 功能：验证 Auditor 会拒绝循环依赖计划，且不会注册或调用旧 TaskManager 与直接工具调用路径。
# 设计：以循环 DAG 替身验证失败汇总和零执行步，确保非法规划在进入 Executor 前被机器规则阻断。
def test_agent_runner_rejects_invalid_plan_before_execution(tmp_path: Path) -> None:
    runner = AgentRunner(
        ManiusConfig(max_steps=3),
        runs_dir=tmp_path / "runs",
        provider_factory=lambda _bus: InvalidPlanProvider(),
    )

    summary = asyncio.run(runner.run("Invalid plan"))
    run_dir = tmp_path / "runs" / summary.run_id
    event_types = [json.loads(line)["type"] for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]

    assert summary.status == "failed"
    assert summary.total_steps == 0
    assert "acyclic" in (summary.reason or "")
    assert not (run_dir / ".tasks").exists()
    assert event_types == ["run_started", "plan_proposed", "run_finished"]


# 功能：验证守护进程运行器只持久化和广播事件，不向本地标准输出泄漏规划或工具日志。
# 设计：使用成功替身并捕获 stdout，保证新增五层事件不会破坏 daemon 与 CLI/TUI 的职责隔离。
def test_agent_runner_does_not_print_local_events(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / "README.md").write_text("# Main sections\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runner = AgentRunner(
        ManiusConfig(max_steps=3),
        runs_dir=tmp_path / "runs",
        provider_factory=lambda _bus: ReadmeProvider(),
    )

    summary = asyncio.run(runner.run("Read README"))

    assert summary.status == "success"
    assert capsys.readouterr().out == ""
