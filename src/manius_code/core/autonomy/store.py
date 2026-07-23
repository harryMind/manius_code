from __future__ import annotations

import json
import os
import time
from pathlib import Path

from manius_code.core.autonomy.contracts import Plan, PlanStep, StepResult

_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_RETRY_SECONDS = 0.05


class PlanStoreLockError(RuntimeError):
    pass


class PlanStore:
    # 初始化单次运行的计划版本、状态和尝试记录目录。
    def __init__(self, run_dir: Path) -> None:
        self._directory = run_dir / "plan"
        self._directory.mkdir(parents=True, exist_ok=True)
        self._attempts_path = self._directory / "attempts.jsonl"
        self._lock_path = self._directory / ".lock"
        self._lock_descriptor: int | None = None

    # 持久化新的不可变计划版本并刷新当前状态快照。
    def persist(self, plan: Plan) -> None:
        self._acquire_lock()
        try:
            self._write_text_atomically(self._plan_path(plan.version), plan.model_dump_json(indent=2) + "\n")
            self._write_state(plan)
        finally:
            self._release_lock()

    # 记录步骤状态变更后的当前计划快照。
    def save_state(self, plan: Plan) -> None:
        self._acquire_lock()
        try:
            self._write_state(plan)
        finally:
            self._release_lock()

    # 追加一条工具执行或验收尝试事实供恢复和审计使用。
    def record_attempt(self, result: StepResult) -> None:
        self._acquire_lock()
        try:
            with self._attempts_path.open("a", encoding="utf-8") as file:
                file.write(result.model_dump_json() + "\n")
                file.flush()
        finally:
            self._release_lock()

    # 从状态快照和对应不可变计划版本重建可继续调度的计划。
    def load(self) -> Plan:
        self._acquire_lock()
        try:
            state_path = self._directory / "state.json"
            if not state_path.is_file():
                raise FileNotFoundError(f"plan state not found: {state_path}")
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                version = state["version"]
                plan_id = state["plan_id"]
                persisted_steps = state["steps"]
            except (json.JSONDecodeError, KeyError, TypeError) as error:
                raise ValueError(f"invalid plan state: {state_path}") from error
            if not isinstance(version, int) or not isinstance(plan_id, str) or not isinstance(persisted_steps, list):
                raise ValueError(f"invalid plan state: {state_path}")
            plan_path = self._plan_path(version)
            if not plan_path.is_file():
                raise FileNotFoundError(f"plan version not found: {plan_path}")
            try:
                plan = Plan.model_validate_json(plan_path.read_text(encoding="utf-8"))
                steps = [PlanStep.model_validate(step) for step in persisted_steps]
            except ValueError as error:
                raise ValueError(f"invalid persisted plan: {plan_path}") from error
            if plan.plan_id != plan_id or [step.id for step in plan.steps] != [step.id for step in steps]:
                raise ValueError("plan state does not match its persisted version")
            return plan.model_copy(update={"steps": steps})
        finally:
            self._release_lock()

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
        self._write_text_atomically(
            self._directory / "state.json",
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
        )

    # 以临时文件替换方式写入快照，避免读取方看到半写入的 JSON 内容。
    def _write_text_atomically(self, path: Path, content: str) -> None:
        temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary_path.write_text(content, encoding="utf-8")
        os.replace(temporary_path, path)

    # 获取跨进程独占锁以串行化同一运行目录的状态与尝试记录写入。
    def _acquire_lock(self) -> None:
        if self._lock_descriptor is not None:
            raise PlanStoreLockError("plan store lock is not reentrant")
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                descriptor = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError as error:
                if time.monotonic() >= deadline:
                    raise PlanStoreLockError(f"timed out waiting for plan store lock: {self._lock_path}") from error
                time.sleep(_LOCK_RETRY_SECONDS)
                continue
            try:
                os.write(descriptor, str(os.getpid()).encode("ascii"))
            except OSError:
                os.close(descriptor)
                self._lock_path.unlink(missing_ok=True)
                raise
            self._lock_descriptor = descriptor
            return

    # 释放当前持有的跨进程独占锁并删除锁文件。
    def _release_lock(self) -> None:
        if self._lock_descriptor is None:
            raise PlanStoreLockError("plan store lock is not held")
        try:
            os.close(self._lock_descriptor)
        finally:
            self._lock_descriptor = None
            self._lock_path.unlink(missing_ok=True)
