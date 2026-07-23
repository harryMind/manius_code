import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from manius_code.core.tools.invocation import ToolExecutionError

_MAX_BASH_OUTPUT_BYTES = 64 * 1024


class BashArguments(BaseModel):
    command: str = Field(min_length=1)


class BashTool:
    name = "bash"
    arguments_model = BashArguments
    definition = {
        "name": name,
        "description": "Run one shell command in the workspace. Returns combined stdout and stderr, capped at 64KB.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "Shell command to execute in the workspace."}},
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
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(Path.cwd()),
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
