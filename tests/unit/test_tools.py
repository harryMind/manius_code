import asyncio
from pathlib import Path

import pytest

from manius_code.core.events.bus import EventBus
from manius_code.core.bus.events import AgentEvent
from manius_code.core.tools.invocation import ToolExecutionError, ToolInvoker
from manius_code.core.tools.read_file import ReadFileTool
from manius_code.core.tools.registry import ToolRegistry


# 功能：验证统一工具调用会广播缺失文件的具体失败信息。
# 设计：通过 ToolInvoker.invoke 而非直接执行工具，断言事件包装与 ReadFileTool 的错误转换同时生效。
def test_read_file_missing_path_emits_specific_failure_event(tmp_path: Path) -> None:
    events: list[AgentEvent] = []
    event_bus = EventBus()
    event_bus.subscribe(events.append)
    tools = ToolRegistry()
    tools.register(ReadFileTool())
    invoker = ToolInvoker(tools, event_bus, "run-1", lambda: 1)
    missing_path = tmp_path / "missing.txt"
    with pytest.raises(ToolExecutionError, match="file not found"):
        asyncio.run(invoker.invoke("read_file", {"path": str(missing_path)}))
    assert [event.type for event in events] == ["tool_call_start", "tool_call_failed"]
    assert events[-1].error == f"file not found: {missing_path}"
