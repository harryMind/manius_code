from manius_code.core.autonomy.contracts import Plan, PlanStep


class Scheduler:
    # 根据成功依赖刷新步骤就绪状态并返回最小编号的可执行步骤。
    def next_ready_step(self, plan: Plan) -> PlanStep | None:
        candidates = [*self.ready_steps(plan), *(step for step in plan.steps if step.status == "retryable")]
        return min(candidates, key=lambda step: step.id) if candidates else None

    # 判断是否所有计划步骤均已通过系统验证。
    def is_complete(self, plan: Plan) -> bool:
        return all(step.status == "succeeded" for step in plan.steps)

    # 刷新依赖已满足的步骤并按稳定标识批量返回可并行调度的就绪步骤。
    def ready_steps(self, plan: Plan) -> list[PlanStep]:
        self._promote_ready_steps(plan)
        return sorted((step for step in plan.steps if step.status == "ready"), key=lambda step: step.id)

    # 将所有依赖均成功的待执行步骤提升为就绪状态。
    def _promote_ready_steps(self, plan: Plan) -> None:
        completed = {step.id for step in plan.steps if step.status == "succeeded"}
        for step in plan.steps:
            if step.status == "pending" and set(step.dependencies).issubset(completed):
                step.status = "ready"
