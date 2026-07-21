import asyncio
import json
from pathlib import Path

import pytest

from manius_code.core.tasks.manager import TaskManager
from manius_code.core.tools.invocation import ToolExecutionError
from manius_code.core.tools.task_tools import TaskCreateTool, TaskGetTool, TaskUpdateTool


# 功能：验证任务管理器会持久化独立 JSON 文件，并在前置任务完成时解除下游依赖。
# 设计：用两个具备显式依赖关系的任务覆盖自增 ID、状态更新、级联解锁和紧凑列表输出。
def test_task_manager_persists_and_unlocks_dependent_tasks(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path / ".tasks")
    research = manager.create("Inspect project", "Review the current structure")
    delivery = manager.create("Write report", blocked_by=[research.id])

    assert manager.list() == "[] 1 Inspect project\n[] 2 Write report (blocked_by: 1)"
    manager.update(research.id, status="completed")

    unlocked = manager.get(delivery.id)
    persisted = json.loads((tmp_path / ".tasks" / "task_2.json").read_text(encoding="utf-8"))
    assert unlocked.blocked_by == []
    assert persisted["blocked_by"] == []
    assert manager.list() == "[x] 1 Inspect project\n[] 2 Write report"
    assert (tmp_path / ".tasks" / "task_1.json").is_file()


# 功能：验证任务工具共享任务管理器，并将非法任务查询转换成可展示的工具错误。
# 设计：通过真实工具适配器完成创建和更新，再查询不存在 ID 以覆盖正常与失败两条调用路径。
def test_task_tools_share_manager_and_return_specific_errors(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path / ".tasks")
    create_tool = TaskCreateTool(manager)
    update_tool = TaskUpdateTool(manager)
    get_tool = TaskGetTool(manager)

    created = json.loads(asyncio.run(create_tool.execute({"subject": "Implement feature"})))
    updated = json.loads(asyncio.run(update_tool.execute({"task_id": created["id"], "status": "in_progress"})))

    assert created["id"] == 1
    assert updated["status"] == "in_progress"
    with pytest.raises(ToolExecutionError, match="task not found: 99"):
        asyncio.run(get_tool.execute({"task_id": 99}))
