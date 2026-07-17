from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from manius_code.core.tools.invocation import ToolExecutionError


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

    # 读取 UTF-8 文本文件并转换底层异常为工具错误。
    async def execute(self, arguments: dict[str, Any]) -> str:
        try:
            path = Path(ReadFileArguments.model_validate(arguments).path)
        except ValidationError as error:
            raise ToolExecutionError(self.name, "requires a valid 'path' string") from error
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise ToolExecutionError(self.name, f"file not found: {path}") from error
        except IsADirectoryError as error:
            raise ToolExecutionError(self.name, f"path is a directory, not a text file: {path}") from error
        except PermissionError as error:
            raise ToolExecutionError(self.name, f"permission denied: {path}") from error
        except UnicodeDecodeError as error:
            raise ToolExecutionError(self.name, f"file is not valid UTF-8 text: {path}") from error
        except OSError as error:
            raise ToolExecutionError(self.name, f"could not read file {path}: {error}") from error
