import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from manius_code.core.tools.invocation import ToolExecutionError
from manius_code.core.tools.paths import resolve_workspace_path


class WriteFileArguments(BaseModel):
    path: str
    content: str


class ListDirectoryArguments(BaseModel):
    path: str = "."
    max_depth: int = Field(default=1, ge=0, le=8)


# 将已校验的工作区路径转换为稳定的相对显示名称。
def _workspace_label(path: Path, workspace: Path) -> str:
    return path.relative_to(workspace).as_posix() or "."


# 在线程池中创建父目录并写入 UTF-8 文件，避免阻塞事件循环。
def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class WriteFileTool:
    name = "write_file"
    arguments_model = WriteFileArguments
    definition = {
        "name": name,
        "description": "Write UTF-8 text to a file within the workspace and create missing parent directories.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative output file path."},
                "content": {"type": "string", "description": "Full UTF-8 text content to write."},
            },
            "required": ["path", "content"],
        },
    }

    # 注入任务工作区以隔离文件写入范围而不改变 daemon 进程的当前目录。
    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = (workspace or Path.cwd()).expanduser().resolve()

    # 写入文本文件并保证目标路径始终位于当前工作区内。
    async def execute(self, arguments: dict[str, Any]) -> str:
        try:
            values = WriteFileArguments.model_validate(arguments)
            path = resolve_workspace_path(values.path, self._workspace)
        except ValidationError as error:
            raise ToolExecutionError(self.name, "requires valid 'path' and 'content' strings") from error
        except ValueError as error:
            raise ToolExecutionError(self.name, str(error)) from error
        try:
            await asyncio.to_thread(_write_text, path, values.content)
            return f"wrote {len(values.content)} characters to {_workspace_label(path, self._workspace)}"
        except IsADirectoryError as error:
            raise ToolExecutionError(self.name, f"path is a directory, not a file: {path}") from error
        except PermissionError as error:
            raise ToolExecutionError(self.name, f"permission denied: {path}") from error
        except OSError as error:
            raise ToolExecutionError(self.name, f"could not write file {path}: {error}") from error


class ListDirTool:
    name = "list_dir"
    arguments_model = ListDirectoryArguments
    definition = {
        "name": name,
        "description": "List files and directories within the workspace with a bounded recursive depth.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative directory path."},
                "max_depth": {"type": "integer", "minimum": 0, "maximum": 8, "description": "Nested directory levels to include."},
            },
        },
    }

    # 注入任务工作区以隔离目录遍历范围而不改变 daemon 进程的当前目录。
    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = (workspace or Path.cwd()).expanduser().resolve()

    # 列出受限深度的目录树，同时避免沿符号链接进入工作区外。
    async def execute(self, arguments: dict[str, Any]) -> str:
        try:
            values = ListDirectoryArguments.model_validate(arguments)
            path = resolve_workspace_path(values.path, self._workspace)
        except ValidationError as error:
            raise ToolExecutionError(self.name, "requires a valid 'path' and max_depth between 0 and 8") from error
        except ValueError as error:
            raise ToolExecutionError(self.name, str(error)) from error
        if not path.exists():
            raise ToolExecutionError(self.name, f"directory not found: {path}")
        if not path.is_dir():
            raise ToolExecutionError(self.name, f"path is not a directory: {path}")
        try:
            entries = await asyncio.to_thread(self._collect, path, values.max_depth)
            header = f"{_workspace_label(path, self._workspace)}/"
            return "\n".join([header, *entries]) if entries else f"{header}\n(empty)"
        except PermissionError as error:
            raise ToolExecutionError(self.name, f"permission denied: {path}") from error
        except OSError as error:
            raise ToolExecutionError(self.name, f"could not list directory {path}: {error}") from error

    # 按名称稳定排序递归收集目录条目并使用缩进表达层级。
    def _collect(self, directory: Path, max_depth: int, prefix: str = "") -> list[str]:
        lines: list[str] = []
        for entry in sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            suffix = "/" if entry.is_dir() and not entry.is_symlink() else ""
            lines.append(f"{prefix}{entry.name}{suffix}")
            if entry.is_dir() and not entry.is_symlink() and max_depth > 0:
                lines.extend(self._collect(entry, max_depth - 1, f"{prefix}  "))
        return lines
