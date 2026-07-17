from typing import Any, Literal

from pydantic import BaseModel, Field

# s1阶段仅保留 AgentLoop 运行必需最小字段
class ExecutionContext(BaseModel):
    run_id: str
    goal: str
    step: int = 0
    status: Literal["running", "success", "failed"] = "running"
    reason: str | None = None
    result: str = ""
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

    # 将运行中的任务标记为成功并保存最终结果。
    def mark_success(self, result: str = "") -> None:
        if self.status != "running":
            raise ValueError(f"Cannot mark {self.status} task as success")
        self.status = "success"
        self.result = result
        self.reason = None

    # 将运行中的任务标记为失败并保存失败原因。
    def mark_failed(self, reason: str) -> None:
        if self.status != "running":
            raise ValueError(f"Cannot mark {self.status} task as failed")
        self.status = "failed"
        self.reason = reason

    # 判断任务是否已进入成功或失败的终止状态。
    def is_done(self) -> bool:
        return self.status in {"success", "failed"}
