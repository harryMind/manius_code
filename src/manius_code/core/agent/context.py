from typing import Any

from pydantic import BaseModel, Field


class ExecutionContext(BaseModel):
    run_id: str
    goal: str
    step: int = 0
    messages: list[dict[str, Any]] = Field(default_factory=list)

    # 以用户目标初始化第一条 Claude 对话消息。
    def initialize(self) -> None:
        self.messages.append({"role": "user", "content": self.goal})

    # 记录 Claude 的 assistant 内容块以维持多轮上下文。
    def add_assistant_response(self, content: list[dict[str, Any]]) -> None:
        self.messages.append({"role": "assistant", "content": content})

    # 记录一次工具观察结果，供下一轮 Claude 调用使用。
    def add_tool_result(self, tool_use_id: str, result: str) -> None:
        self.messages.append(
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": result}],
            }
        )
