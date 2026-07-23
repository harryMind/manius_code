import asyncio
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from manius_code.core.tools.invocation import ToolExecutionError

_MAX_BASH_OUTPUT_BYTES = 64 * 1024


# 为带引号的可执行文件路径补充 PowerShell 所需的调用运算符。
def _powershell_command(command: str) -> str:
    leading = command[: len(command) - len(command.lstrip())]
    body = command.lstrip()
    if body.startswith(('"', "'")):
        command = f"{leading}& {body}"
    return f"{command}; if ($null -ne $LASTEXITCODE) {{ exit $LASTEXITCODE }}"


# 根据宿主操作系统返回实际执行命令所需的 Shell 参数。
def _shell_arguments(command: str) -> tuple[str, ...]:
    if os.name == "nt":
        return ("powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _powershell_command(command))
    return ("/bin/sh", "-lc", command)


# 返回供模型和调用方识别的当前 Shell 名称。
def _shell_name() -> str:
    return "PowerShell" if os.name == "nt" else "POSIX shell"


class BashArguments(BaseModel):
    command: str = Field(min_length=1)


class BashTool:
    name = "bash"
    arguments_model = BashArguments
    definition: dict[str, Any]

    # 注入工作区并暴露与宿主系统一致的 Shell 调用说明。
    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = (workspace or Path.cwd()).expanduser().resolve()
        self.definition = {
            "name": self.name,
            "description": (
                f"Run one {_shell_name()} command in the workspace. Returns combined stdout and stderr, capped at 64KB."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": f"{_shell_name()} command to execute in the workspace."}
                },
                "required": ["command"],
            },
        }

    # 在工作区内执行命令并将合并输出限制为 64KB。
    async def execute(self, arguments: dict[str, Any]) -> str:
        try:
            command = BashArguments.model_validate(arguments).command
        except ValidationError as error:
            raise ToolExecutionError(self.name, "requires a non-empty 'command' string") from error
        try:
            process = await asyncio.create_subprocess_exec(
                *_shell_arguments(command),
                cwd=str(self._workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            if process.stdout is None:
                raise RuntimeError("command output pipe was not created")
            output = bytearray()
            truncated = False
            while chunk := await process.stdout.read(4096):
                remaining = _MAX_BASH_OUTPUT_BYTES - len(output)
                if remaining > 0:
                    output.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True
            await process.wait()
        except (OSError, RuntimeError) as error:
            raise ToolExecutionError(self.name, f"could not start command: {error}") from error
        text = output.decode("utf-8", errors="replace")
        if truncated:
            text += "\n[truncated: command output exceeds 64KB]"
        if process.returncode != 0:
            detail = text.strip() or "(no output)"
            raise ToolExecutionError(self.name, f"command exited with code {process.returncode}: {detail}")
        return f"exit_code=0\n{text}" if text else "exit_code=0"
