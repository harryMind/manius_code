from manius_code.core.autonomy.contracts import Plan, PlanStep


class Scheduler:
    # 根据成功依赖刷新步骤就绪状态并返回最小编号的可执行步骤。
    def next_ready_step(self, plan: Plan) -> PlanStep | None:
        completed = {step.id for step in plan.steps if step.status == "succeeded"}
        for step in plan.steps:
            if step.status == "pending" and set(step.dependencies).issubset(completed):
                step.status = "ready"
        ready_steps = [step for step in plan.steps if step.status in {"ready", "retryable"}]
        return min(ready_steps, key=lambda step: step.id) if ready_steps else None

    # 判断是否所有计划步骤均已通过系统验证。
    def is_complete(self, plan: Plan) -> bool:
        return all(step.status == "succeeded" for step in plan.steps)
