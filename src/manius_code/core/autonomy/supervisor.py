from __future__ import annotations

from pathlib import Path

from manius_code.core.agent.context import ExecutionContext
from manius_code.core.autonomy.auditor import AuditError, Auditor
from manius_code.core.autonomy.contracts import Plan, PlanProposal, PlanStep, StepResult
from manius_code.core.autonomy.executor import Executor
from manius_code.core.autonomy.planner import AutonomyProvider, Planner
from manius_code.core.autonomy.policy import AutonomyPolicy
from manius_code.core.autonomy.resolver import Resolver
from manius_code.core.autonomy.scheduler import Scheduler
from manius_code.core.autonomy.store import PlanStore
from manius_code.core.autonomy.verifier import Verifier
from manius_code.core.bus.events import (
    PlanApprovedEvent,
    PlanProposedEvent,
    PlanRevisedEvent,
    StepDoneEvent,
    StepPlanningEvent,
    StepVerifiedEvent,
)
from manius_code.core.events.bus import EventBus
from manius_code.core.memory.store import MemoryStore


class AutonomousSupervisor:
    # 组装五层闭环所需的状态、策略、基础执行器和可复用持久化组件。
    def __init__(
        self,
        context: ExecutionContext,
        provider: AutonomyProvider,
        event_bus: EventBus,
        run_dir: Path,
        workspace: Path,
        policy: AutonomyPolicy,
    ) -> None:
        self._context = context
        self._provider = provider
        self._event_bus = event_bus
        self._policy = policy
        self._planner = Planner(provider)
        self._resolver = Resolver(provider)
        self._executor = Executor(context.run_id, event_bus)
        self._auditor = Auditor(self._executor.tool_names())
        self._scheduler = Scheduler()
        self._verifier = Verifier()
        self._plans = PlanStore(run_dir)
        self._memory = MemoryStore(run_dir, workspace)
        self._history: list[StepResult] = []

    # 驱动计划、审计、调度、执行、验证、修复和记忆写入直到任务终态。
    async def run(self) -> None:
        proposal = await self._planner.create(
            self._context.run_id,
            self._context.step,
            self._context.goal,
            self._memory.retrieve(),
            sorted(self._executor.tool_names()),
        )
        plan = await self._approve_plan(proposal, 1)
        while self._context.step < self._policy.max_steps:
            ready_step = self._scheduler.next_ready_step(plan)
            self._plans.save_state(plan)
            if ready_step is None:
                if self._scheduler.is_complete(plan):
                    await self._finish_success(plan)
                    return
                raise RuntimeError("plan has no executable step; unresolved dependencies remain")
            replacement = await self._run_step(plan, ready_step)
            if replacement is not None:
                plan = replacement
        raise RuntimeError(f"Agent exceeded max_steps={self._policy.max_steps}")

    # 将经审计的计划提案版本化并持久化为当前运行的唯一计划事实。
    async def _approve_plan(self, proposal: PlanProposal, version: int) -> Plan:
        await self._event_bus.publish(
            PlanProposedEvent(
                run_id=self._context.run_id,
                step=self._context.step,
                version=version,
                plan=proposal.model_dump(mode="json"),
            )
        )
        if proposal.goal != self._context.goal:
            raise AuditError("plan goal must match the active run goal")
        self._auditor.approve_plan(proposal)
        plan = Plan(version=version, goal=proposal.goal, steps=proposal.steps)
        self._plans.persist(plan)
        await self._event_bus.publish(
            PlanApprovedEvent(
                run_id=self._context.run_id,
                step=self._context.step,
                version=plan.version,
                plan_id=plan.plan_id,
            )
        )
        return plan

    # 执行一个调度步骤，并在每次操作后用验证或修复结果更新其状态。
    async def _run_step(self, plan: Plan, plan_step: PlanStep) -> Plan | None:
        self._context.step += 1
        plan_step.status = "running"
        plan_step.attempt_count += 1
        await self._event_bus.publish(
            StepPlanningEvent(
                run_id=self._context.run_id,
                step=self._context.step,
                plan=f"{plan_step.title}: {plan_step.description}",
            )
        )
        try:
            proposal = await self._planner.propose_action(
                self._context.run_id,
                self._context.step,
                plan_step,
                self._history,
            )
            self._auditor.approve_action(plan_step, proposal)
            result = await self._executor.execute(proposal, self._context.step, plan_step.attempt_count)
        except (AuditError, RuntimeError) as error:
            result = StepResult(step_id=plan_step.id, attempt=plan_step.attempt_count, error=str(error))
        self._history.append(result)
        self._plans.record_attempt(result)
        if result.error is None:
            plan_step.status = "verifying"
            verification = self._verifier.verify(plan_step, result)
            if verification.passed:
                plan_step.status = "succeeded"
                observation = "; ".join(verification.evidence) or "acceptance criteria verified"
                await self._event_bus.publish(
                    StepVerifiedEvent(
                        run_id=self._context.run_id,
                        step=self._context.step,
                        step_id=plan_step.id,
                        evidence=verification.evidence,
                    )
                )
                await self._publish_step_done(observation)
                self._plans.save_state(plan)
                return None
            result.error = verification.reason or "verification failed"
            self._history[-1] = result
            self._plans.record_attempt(result)
        plan_step.last_error = result.error
        replacement = await self._repair(plan, plan_step, result)
        if replacement is not None:
            return replacement
        self._plans.save_state(plan)
        return None

    # 依据 Resolver 决策修改步骤状态、重建计划或以明确原因终止运行。
    async def _repair(self, plan: Plan, plan_step: PlanStep, result: StepResult) -> Plan | None:
        if plan_step.attempt_count >= self._policy.max_attempts_per_step:
            plan_step.status = "failed"
            raise RuntimeError(f"step {plan_step.id} exceeded max_attempts={self._policy.max_attempts_per_step}: {result.error}")
        decision = await self._resolver.decide(
            self._context.run_id,
            self._context.step,
            self._context.goal,
            plan_step,
            result,
            self._history,
        )
        if decision.action in {"retry", "revise_step"}:
            plan_step.status = "retryable"
            await self._publish_step_done(f"repair scheduled: {decision.reason}")
            return None
        if decision.action == "replan":
            if decision.plan is None:
                raise RuntimeError("resolver requested replan without a plan")
            if plan.version >= self._policy.max_plan_versions:
                raise RuntimeError(f"plan exceeded max_versions={self._policy.max_plan_versions}")
            replacement = await self._approve_plan(decision.plan, plan.version + 1)
            await self._event_bus.publish(
                PlanRevisedEvent(
                    run_id=self._context.run_id,
                    step=self._context.step,
                    previous_version=plan.version,
                    version=replacement.version,
                    reason=decision.reason,
                )
            )
            await self._publish_step_done(f"plan revised: {decision.reason}")
            return replacement
        plan_step.status = "failed"
        raise RuntimeError(f"resolver aborted step {plan_step.id}: {decision.reason}")

    # 发布完成步骤的统一事件，供 CLI、TUI、追踪和事件持久化复用。
    async def _publish_step_done(self, observation: str) -> None:
        await self._event_bus.publish(
            StepDoneEvent(
                run_id=self._context.run_id,
                step=self._context.step,
                complete=False,
                observation=observation,
            )
        )

    # 仅当全部步骤均经验证后汇总结果并写入受工作区隔离的记忆。
    async def _finish_success(self, plan: Plan) -> None:
        summary = await self._provider.summarize(
            self._context.run_id,
            self._context.step,
            self._context.goal,
            PlanProposal(goal=plan.goal, steps=plan.steps),
            self._history,
        )
        self._context.mark_success(summary)
        self._memory.record_verified(self._context.goal, summary, plan, self._history)
