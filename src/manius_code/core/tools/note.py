from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from manius_code.core.sessions.models import SessionNote
from manius_code.core.tools.invocation import ToolExecutionError

NoteSaver = Callable[[str, str, list[str]], Awaitable[SessionNote]]


class NoteSaveArguments(BaseModel):
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)


class NoteSaveTool:
    name = "note_save"
    arguments_model = NoteSaveArguments

    # 注入会话边界内的笔记写入回调，使工具不感知文件路径和会话管理实现。
    def __init__(self, save_note: NoteSaver) -> None:
        self._save_note = save_note

    # 校验笔记参数并委托会话层创建长期笔记，返回可供 Agent 复用的简短确认信息。
    async def execute(self, arguments: dict[str, Any]) -> str:
        try:
            values = NoteSaveArguments.model_validate(arguments)
            note = await self._save_note(values.title, values.content, values.tags)
        except ValidationError as error:
            raise ToolExecutionError(self.name, "requires non-empty title and content, with optional string tags") from error
        except Exception as error:
            raise ToolExecutionError(self.name, f"could not save note: {error}") from error
        return f"saved note {note.id}: {note.title}"
