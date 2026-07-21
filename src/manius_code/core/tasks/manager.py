from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

TaskStatus = Literal["pending", "in_progress", "completed"]


class TaskRecord(BaseModel):
    id: int
    subject: str
    description: str = ""
    status: TaskStatus = "pending"
    blocked_by: list[int] = Field(default_factory=list)
    created_at: str
    updated_at: str


class TaskManager:
    # 初始化单次运行独占的任务文件存储目录。
    def __init__(self, tasks_dir: Path) -> None:
        self._tasks_dir = tasks_dir
        self._tasks_dir.mkdir(parents=True, exist_ok=True)

    # 创建一条带有可选依赖关系的待办任务并写入独立 JSON 文件。
    def create(
        self,
        subject: str,
        description: str = "",
        blocked_by: list[int] | None = None,
    ) -> TaskRecord:
        if not subject.strip():
            raise ValueError("task subject must not be empty")
        timestamp = self._timestamp()
        task = TaskRecord(
            id=self._next_id(),
            subject=subject,
            description=description,
            blocked_by=list(dict.fromkeys(blocked_by or [])),
            created_at=timestamp,
            updated_at=timestamp,
        )
        self._write(task)
        return task

    # 更新任务状态或依赖集合，并在完成时自动解除下游任务依赖。
    def update(
        self,
        task_id: int,
        status: TaskStatus | None = None,
        add_blocked_by: list[int] | None = None,
        remove_blocked_by: list[int] | None = None,
    ) -> TaskRecord:
        task = self.get(task_id)
        blocked_by = list(task.blocked_by)
        for dependency_id in add_blocked_by or []:
            if dependency_id not in blocked_by:
                blocked_by.append(dependency_id)
        for dependency_id in remove_blocked_by or []:
            if dependency_id in blocked_by:
                blocked_by.remove(dependency_id)
        task = TaskRecord.model_validate(
            {
                **task.model_dump(),
                "status": status or task.status,
                "blocked_by": blocked_by,
                "updated_at": self._timestamp(),
            }
        )
        self._write(task)
        if task.status == "completed":
            self.clear_dependency(task.id)
        return task

    # 扫描全部任务并移除已完成任务作为阻塞依赖的引用。
    def clear_dependency(self, completed_id: int) -> None:
        for task in self._all_tasks():
            if completed_id not in task.blocked_by:
                continue
            task.blocked_by.remove(completed_id)
            task.updated_at = self._timestamp()
            self._write(task)

    # 以紧凑文本返回任务状态和仍未解除的依赖关系。
    def list(self) -> str:
        markers = {"pending": "[]", "in_progress": "[>]", "completed": "[x]"}
        lines = []
        for task in self._all_tasks():
            dependencies = f" (blocked_by: {', '.join(map(str, task.blocked_by))})" if task.blocked_by else ""
            lines.append(f"{markers[task.status]} {task.id} {task.subject}{dependencies}")
        return "\n".join(lines) if lines else "(no tasks)"

    # 读取指定任务的完整持久化记录。
    def get(self, task_id: int) -> TaskRecord:
        path = self._task_path(task_id)
        if not path.is_file():
            raise KeyError(f"task not found: {task_id}")
        try:
            return TaskRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise OSError(f"could not read task {task_id}: {error}") from error

    # 返回按整数任务 ID 排序的所有任务文件路径。
    def _task_paths(self) -> list[Path]:
        paths: list[tuple[int, Path]] = []
        for path in self._tasks_dir.glob("task_*.json"):
            try:
                task_id = int(path.stem.removeprefix("task_"))
            except ValueError:
                continue
            paths.append((task_id, path))
        return [path for _, path in sorted(paths)]

    # 读取并按任务 ID 排序返回全部任务记录。
    def _all_tasks(self) -> list[TaskRecord]:
        return [self.get(int(path.stem.removeprefix("task_"))) for path in self._task_paths()]

    # 计算下一个连续可用的整数任务 ID。
    def _next_id(self) -> int:
        paths = self._task_paths()
        if not paths:
            return 1
        return max(int(path.stem.removeprefix("task_")) for path in paths) + 1

    # 根据任务 ID 生成固定命名约定的 JSON 文件路径。
    def _task_path(self, task_id: int) -> Path:
        if task_id < 1:
            raise ValueError("task_id must be a positive integer")
        return self._tasks_dir / f"task_{task_id}.json"

    # 将任务模型以可人工审阅的 JSON 格式写入其独立文件。
    def _write(self, task: TaskRecord) -> None:
        self._task_path(task.id).write_text(
            json.dumps(task.model_dump(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    # 生成 UTC ISO 8601 时间戳供任务创建和更新记录复用。
    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat()
