import json

import pytest

from manius_code.core.autonomy.contracts import Plan, PlanStep, StepResult
from manius_code.core.autonomy.scheduler import Scheduler
from manius_code.core.autonomy.store import PlanStore, PlanStoreLockError


# 功能：验证批量接口会提升依赖已满足的步骤并稳定返回全部 ready 步骤。
# 设计：同时构造已完成、待解锁和 retryable 步骤，确保并行候选不会混入串行重试队列。
def test_scheduler_returns_all_ready_steps_in_stable_order() -> None:
    plan = Plan(
        version=1,
        goal="schedule",
        steps=[
            PlanStep(id="done", title="Done", status="succeeded"),
            PlanStep(id="write", title="Write", dependencies=["done"]),
            PlanStep(id="inspect", title="Inspect"),
            PlanStep(id="retry", title="Retry", status="retryable"),
        ],
    )

    ready = Scheduler().ready_steps(plan)

    assert [step.id for step in ready] == ["inspect", "write"]
    assert plan.steps[1].status == "ready"
    assert Scheduler().next_ready_step(plan).id == "inspect"


# 功能：验证 PlanStore 能从不可变计划版本和最新状态快照恢复完整的可调度计划。
# 设计：先持久化初始计划，再修改状态并记录尝试，断言 load 保留元数据且采用最新步骤状态。
def test_plan_store_load_restores_latest_step_state(tmp_path) -> None:
    store = PlanStore(tmp_path)
    plan = Plan(version=1, goal="resume", steps=[PlanStep(id="one", title="One")])
    store.persist(plan)
    plan.steps[0].status = "retryable"
    plan.steps[0].attempt_count = 1
    plan.steps[0].last_error = "temporary failure"
    store.save_state(plan)
    store.record_attempt(StepResult(step_id="one", attempt=1, error="temporary failure"))

    restored = store.load()

    assert restored.plan_id == plan.plan_id
    assert restored.goal == "resume"
    assert restored.steps[0].status == "retryable"
    assert restored.steps[0].attempt_count == 1
    assert json.loads((tmp_path / "plan" / "attempts.jsonl").read_text(encoding="utf-8"))["error"] == "temporary failure"


# 功能：验证并发访问同一运行目录时第二个 PlanStore 会超时失败而不会写入竞争数据。
# 设计：由第一个实例持有 lock 文件并将等待时间设为零，断言第二个实例获得明确锁错误且锁在释放后被清理。
def test_plan_store_rejects_concurrent_writer(tmp_path, monkeypatch) -> None:
    first = PlanStore(tmp_path)
    second = PlanStore(tmp_path)
    plan = Plan(version=1, goal="lock", steps=[PlanStep(id="one", title="One")])
    first._acquire_lock()
    monkeypatch.setattr("manius_code.core.autonomy.store._LOCK_TIMEOUT_SECONDS", 0)
    try:
        with pytest.raises(PlanStoreLockError, match="timed out"):
            second.save_state(plan)
    finally:
        first._release_lock()

    assert not (tmp_path / "plan" / ".lock").exists()
