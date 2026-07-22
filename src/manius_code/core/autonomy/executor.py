from __future__ import annotations

import time
from typing import Any

from manius_code.core.autonomy.contracts import ActionProposal, StepResult
from manius_code.core.bus.events import ToolCallFailedEvent, ToolCallStartEvent, ToolCallSuccessEvent
from manius_code.core.events.bus import EventBus
from manius_code.core.tools.bash import BashTool
from manius_code.core.tools.file_tools import ListDirTool, WriteFileTool
from manius_code.core.tools.invocation import ToolExecutionError
from manius_code.core.tools.read_file import ReadFileTool


class Executor:
    # 注入运行标识、事件总线和新执行路径复用的实际工具实现。
    def __init__(self, run_id: str, event_bus: EventBus) -> None:
        self._run_id = run_id
        self._event_bus = event_bus
        self._tools: dict[str, Any] = {
            "read_file": ReadFileTool(),
            "write_file": WriteFileTool(),
            "list_dir": ListDirTool(),
            "bash": BashTool(),
        }

    # 返回新执行器允许审计的工具名称而不注册旧 ToolRegistry。
    def tool_names(self) -> set[str]:
        return set(self._tools)

    # 审计通过后执行一个步骤动作并发布既有工具调用事件。
    async def execute(self, proposal: ActionProposal, step: int, attempt: int) -> StepResult:
        await self._event_bus.publish(
            ToolCallStartEvent(
                run_id=self._run_id,
                step=step,
                tool_name=proposal.tool_name,
                arguments=proposal.arguments,
            )
        )
        started_at = time.monotonic()
        try:
            tool = self._tools[proposal.tool_name]
            result = await tool.execute(proposal.arguments)
        except Exception as error:
            if isinstance(error, KeyError):
                message = "tool is not available"
            elif isinstance(error, ToolExecutionError):
                message = error.message
            else:
                message = f"unexpected execution error: {error}"
            duration_ms = round((time.monotonic() - started_at) * 1000)
            await self._event_bus.publish(
                ToolCallFailedEvent(
                    run_id=self._run_id,
                    step=step,
                    tool_name=proposal.tool_name,
                    duration_ms=duration_ms,
                    error=message,
                )
            )
            return StepResult(step_id=proposal.step_id, attempt=attempt, tool_name=proposal.tool_name, error=message)
        duration_ms = round((time.monotonic() - started_at) * 1000)
        await self._event_bus.publish(
            ToolCallSuccessEvent(
                run_id=self._run_id,
                step=step,
                tool_name=proposal.tool_name,
                duration_ms=duration_ms,
                result=result,
            )
        )
        return StepResult(step_id=proposal.step_id, attempt=attempt, tool_name=proposal.tool_name, observation=result)
