from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from manius_code.core.tasks.manager import TaskManager, TaskRecord
from manius_code.core.tools.invocation import ToolExecutionError


class TaskCreateArguments(BaseModel):
    subject: str = Field(min_length=1)
    description: str = ""
    blocked_by: list[int] = Field(default_factory=list)


class TaskUpdateArguments(BaseModel):
    task_id: int = Field(ge=1)
    status: Literal["pending", "in_progress", "completed"] | None = None
    add_blocked_by: list[int] = Field(default_factory=list)
    remove_blocked_by: list[int] = Field(default_factory=list)


class TaskGetArguments(BaseModel):
    task_id: int = Field(ge=1)


# 将任务记录转换为可直接提供给 LLM 的完整 JSON 文本。
def _task_json(task: TaskRecord) -> str:
    return task.model_dump_json(indent=2)


class TaskCreateTool:
    name = "task_create"
    definition = {
        "name": name,
        "description": "Create a planned task. Use integer task IDs to express dependencies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Short task title."},
                "description": {"type": "string", "description": "Optional detailed task instructions."},
                "blocked_by": {"type": "array", "items": {"type": "integer"}, "description": "Task IDs that must finish first."},
            },
            "required": ["subject"],
        },
    }

    # 注入单次运行共享的任务管理器。
    def __init__(self, task_manager: TaskManager) -> None:
        self._task_manager = task_manager

    # 校验任务创建参数并持久化新的规划任务。
    async def execute(self, arguments: dict[str, Any]) -> str:
        try:
            values = TaskCreateArguments.model_validate(arguments)
            return _task_json(self._task_manager.create(values.subject, values.description, values.blocked_by))
        except ValidationError as error:
            raise ToolExecutionError(self.name, "requires a non-empty 'subject' and integer dependency IDs") from error
        except (OSError, ValueError) as error:
            raise ToolExecutionError(self.name, str(error)) from error


class TaskUpdateTool:
    name = "task_update"
    definition = {
        "name": name,
        "description": "Update a task status or its blocking dependencies. Mark completed tasks to unlock dependents automatically.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "minimum": 1},
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                "add_blocked_by": {"type": "array", "items": {"type": "integer"}},
                "remove_blocked_by": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["task_id"],
        },
    }

    # 注入单次运行共享的任务管理器。
    def __init__(self, task_manager: TaskManager) -> None:
        self._task_manager = task_manager

    # 校验更新指令并返回状态变更后的完整任务记录。
    async def execute(self, arguments: dict[str, Any]) -> str:
        try:
            values = TaskUpdateArguments.model_validate(arguments)
            task = self._task_manager.update(
                values.task_id,
                values.status,
                values.add_blocked_by,
                values.remove_blocked_by,
            )
            return _task_json(task)
        except ValidationError as error:
            raise ToolExecutionError(self.name, "requires a positive 'task_id' and valid task status") from error
        except KeyError as error:
            raise ToolExecutionError(self.name, str(error)) from error
        except (OSError, ValueError) as error:
            raise ToolExecutionError(self.name, str(error)) from error


class TaskListTool:
    name = "task_list"
    definition = {
        "name": name,
        "description": "List all planned tasks in a compact status format, including unresolved dependencies.",
        "input_schema": {"type": "object", "properties": {}},
    }

    # 注入单次运行共享的任务管理器。
    def __init__(self, task_manager: TaskManager) -> None:
        self._task_manager = task_manager

    # 返回全部任务的 LLM 友好紧凑状态摘要。
    async def execute(self, arguments: dict[str, Any]) -> str:
        try:
            return self._task_manager.list()
        except (OSError, ValueError) as error:
            raise ToolExecutionError(self.name, str(error)) from error


class TaskGetTool:
    name = "task_get"
    definition = {
        "name": name,
        "description": "Get the complete stored details for one planned task.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "integer", "minimum": 1}},
            "required": ["task_id"],
        },
    }

    # 注入单次运行共享的任务管理器。
    def __init__(self, task_manager: TaskManager) -> None:
        self._task_manager = task_manager

    # 校验任务 ID 并返回对应任务的完整 JSON 详情。
    async def execute(self, arguments: dict[str, Any]) -> str:
        try:
            values = TaskGetArguments.model_validate(arguments)
            return _task_json(self._task_manager.get(values.task_id))
        except ValidationError as error:
            raise ToolExecutionError(self.name, "requires a positive integer 'task_id'") from error
        except KeyError as error:
            raise ToolExecutionError(self.name, str(error)) from error
        except (OSError, ValueError) as error:
            raise ToolExecutionError(self.name, str(error)) from error
