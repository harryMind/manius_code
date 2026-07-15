import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from manius_code.core.events.bus import EventBus
from manius_code.core.events.models import ToolCallFailedEvent, ToolCallStartEvent, ToolCallSuccessEvent


class ReadFileArguments(BaseModel):
    path: str


class ReadFileTool:
    name = "read_file"
    definition = {
        "name": name,
        "description": "Read a UTF-8 text file from the local workspace.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path of the text file to read."}},
            "required": ["path"],
        },
    }

    # 注入事件总线和当前运行元数据。
    def __init__(self, event_bus: EventBus, run_id: str, step_getter: Callable[[], int]) -> None:
        self._event_bus = event_bus
        self._run_id = run_id
        self._step_getter = step_getter

    # 读取文本文件，并以事件记录调用结果和耗时。
    async def execute(self, arguments: dict[str, Any]) -> str:
        parsed = ReadFileArguments.model_validate(arguments)
        step = self._step_getter()
        await self._event_bus.publish(
            ToolCallStartEvent(run_id=self._run_id, step=step, tool_name=self.name, arguments=arguments)
        )
        started_at = time.monotonic()
        try:
            result = Path(parsed.path).read_text(encoding="utf-8")
        except Exception as error:
            duration_ms = round((time.monotonic() - started_at) * 1000)
            await self._event_bus.publish(
                ToolCallFailedEvent(
                    run_id=self._run_id,
                    step=step,
                    tool_name=self.name,
                    duration_ms=duration_ms,
                    error=str(error),
                )
            )
            raise
        duration_ms = round((time.monotonic() - started_at) * 1000)
        await self._event_bus.publish(
            ToolCallSuccessEvent(
                run_id=self._run_id,
                step=step,
                tool_name=self.name,
                duration_ms=duration_ms,
                result=result,
            )
        )
        return result
