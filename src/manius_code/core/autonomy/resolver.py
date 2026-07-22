from manius_code.core.autonomy.contracts import PlanStep, ResolverDecision, StepResult
from manius_code.core.autonomy.planner import AutonomyProvider


class Resolver:
    # 注入模型适配器以独立处理失败后的修复建议。
    def __init__(self, provider: AutonomyProvider) -> None:
        self._provider = provider

    # 委托模型在机器规则约束下返回失败修复决策。
    async def decide(
        self,
        run_id: str,
        step: int,
        goal: str,
        plan_step: PlanStep,
        result: StepResult,
        history: list[StepResult],
    ) -> ResolverDecision:
        return await self._provider.resolve(run_id, step, goal, plan_step, result, history)
