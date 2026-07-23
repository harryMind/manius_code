import asyncio
import os
import sys
from pathlib import Path

import pytest

from manius_code.core.events.bus import EventBus
from manius_code.core.bus.events import AgentEvent
from manius_code.core.tools.bash import BashTool, _shell_arguments
from manius_code.core.tools.file_tools import ListDirTool, WriteFileTool
from manius_code.core.tools.invocation import ToolExecutionError, ToolInvoker
from manius_code.core.tools.read_file import ReadFileTool
from manius_code.core.tools.registry import ToolRegistry


# 功能：验证统一工具调用会广播缺失文件的具体失败信息。
# 设计：通过 ToolInvoker.invoke 而非直接执行工具，断言事件包装与 ReadFileTool 的错误转换同时生效。
def test_read_file_missing_path_emits_specific_failure_event(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    events: list[AgentEvent] = []
    event_bus = EventBus()
    event_bus.subscribe(events.append)
    tools = ToolRegistry()
    tools.register(ReadFileTool())
    invoker = ToolInvoker(tools, event_bus, "run-1", lambda: 1)
    missing_path = Path("missing.txt")
    with pytest.raises(ToolExecutionError, match="file not found"):
        asyncio.run(invoker.invoke("read_file", {"path": str(missing_path)}))
    assert [event.type for event in events] == ["tool_call_start", "tool_call_failed"]
    assert events[-1].error == f"file not found: {tmp_path / missing_path}"


# 功能：验证执行类工具可安全读写工作区、限制输出并暴露 Shell 失败原因。
# 设计：在临时工作区执行真实工具，覆盖父目录创建、路径穿越拒绝、读取截断、目录层级和命令执行。
def test_execution_tools_enforce_workspace_boundaries_and_output_limits(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    write_tool = WriteFileTool()
    read_tool = ReadFileTool()
    list_tool = ListDirTool()
    bash_tool = BashTool()

    write_result = asyncio.run(write_tool.execute({"path": "reports/result.txt", "content": "complete"}))
    assert write_result == "wrote 8 characters to reports/result.txt"
    assert asyncio.run(read_tool.execute({"path": "reports/result.txt"})) == "complete"
    assert "reports/" in asyncio.run(list_tool.execute({"path": ".", "max_depth": 1}))
    with pytest.raises(ToolExecutionError, match="path must stay within the workspace"):
        asyncio.run(write_tool.execute({"path": "../outside.txt", "content": "blocked"}))

    (tmp_path / "large.txt").write_bytes(b"a" * (512 * 1024 + 1))
    large_result = asyncio.run(read_tool.execute({"path": "large.txt"}))
    assert large_result.endswith("[truncated: file exceeds 512KB]")
    assert len(large_result) > 512 * 1024

    command = f'"{sys.executable}" -c "print(\'ok\')"'
    assert asyncio.run(bash_tool.execute({"command": command})).splitlines() == ["exit_code=0", "ok"]
    large_command = f'"{sys.executable}" -c "print(\'x\' * 70000)"'
    assert asyncio.run(bash_tool.execute({"command": large_command})).endswith(
        "[truncated: command output exceeds 64KB]"
    )
    with pytest.raises(ToolExecutionError, match="command exited with code 3"):
        asyncio.run(bash_tool.execute({"command": f'"{sys.executable}" -c "import sys; sys.exit(3)"'}))


# 功能：验证文件工具和命令工具使用注入工作区且按宿主系统选择实际 Shell。
# 设计：让进程当前目录与配置工作区不同，断言产物只能写到后者，并直接检查平台分支避免依赖本机命令别名。
def test_execution_tools_use_injected_workspace_and_native_shell(tmp_path: Path, monkeypatch) -> None:
    launcher = tmp_path / "launcher"
    workspace = tmp_path / "agent-output"
    launcher.mkdir()
    workspace.mkdir()
    monkeypatch.chdir(launcher)
    write_tool = WriteFileTool(workspace)
    bash_tool = BashTool(workspace)

    asyncio.run(write_tool.execute({"path": "nested/result.txt", "content": "complete"}))
    command = f'"{sys.executable}" -c "from pathlib import Path; Path(\'from_command.txt\').write_text(\'ok\')"'
    assert asyncio.run(bash_tool.execute({"command": command})).startswith("exit_code=0")

    assert (workspace / "nested" / "result.txt").read_text(encoding="utf-8") == "complete"
    assert (workspace / "from_command.txt").read_text(encoding="utf-8") == "ok"
    assert not (launcher / "nested" / "result.txt").exists()
    shell = _shell_arguments("echo ok")
    if os.name == "nt":
        assert shell[:5] == ("powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command")
        assert "PowerShell" in bash_tool.definition["description"]
    else:
        assert shell[:2] == ("/bin/sh", "-lc")
        assert "POSIX shell" in bash_tool.definition["description"]
