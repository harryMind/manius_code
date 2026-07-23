from typing import Any

from pydantic import BaseModel


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class LlmResponse(BaseModel):
    text: str
    tool_calls: list[ToolCall]
    assistant_content: list[dict[str, Any]]
