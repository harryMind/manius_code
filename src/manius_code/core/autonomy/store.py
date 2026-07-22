from __future__ import annotations

import json
from pathlib import Path

from manius_code.core.autonomy.contracts import Plan, PlanStep, StepResult


class PlanStore:
    # 初始化单次运行的计划版本、状态和尝试记录目录。
    def __init__(self, run_dir: Path) -> None:
        self._directory = run_dir / "plan"
        self._directory.mkdir(parents=True, exist_ok=True)
        self._attempts_path = self._directory / "attempts.jsonl"

    # 持久化新的不可变计划版本并刷新当前状态快照。
    def persist(self, plan: Plan) -> None:
        self._plan_path(plan.version).write_text(
            plan.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        self._write_state(plan)

    # 记录步骤状态变更后的当前计划快照。
    def save_state(self, plan: Plan) -> None:
        self._write_state(plan)

    # 追加一条工具执行或验收尝试事实供恢复和审计使用。
    def record_attempt(self, result: StepResult) -> None:
        with self._attempts_path.open("a", encoding="utf-8") as file:
            file.write(result.model_dump_json() + "\n")

    # 返回当前计划状态中的指定步骤以集中处理不存在错误。
    def step(self, plan: Plan, step_id: str) -> PlanStep:
        for step in plan.steps:
            if step.id == step_id:
                return step
        raise KeyError(f"plan step not found: {step_id}")

    # 生成固定命名的历史计划版本路径。
    def _plan_path(self, version: int) -> Path:
        return self._directory / f"plan.v{version}.json"

    # 将可恢复的当前计划状态写为单独快照。
    def _write_state(self, plan: Plan) -> None:
        (self._directory / "state.json").write_text(
            json.dumps(
                {
                    "plan_id": plan.plan_id,
                    "version": plan.version,
                    "steps": [step.model_dump() for step in plan.steps],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
