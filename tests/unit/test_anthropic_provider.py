import asyncio
from typing import Any

from pydantic import BaseModel

from manius_code.core.config import LlmConfig
from manius_code.core.events.bus import EventBus
from manius_code.core.events.models import AgentEvent
from manius_code.core.llm.anthropic import AnthropicProvider


class FakeBlock(BaseModel):
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] = {}


class FakeResponse(BaseModel):
    content: list[FakeBlock]


class FakeMessages:
    # 保存请求参数并返回确定性的 Claude 内容块。
    def __init__(self) -> None:
        self.request: dict[str, Any] | None = None

    # 模拟 Anthropic messages.create 的异步响应。
    async def create(self, **kwargs: Any) -> FakeResponse:
        self.request = kwargs
        return FakeResponse(
            content=[
                FakeBlock(type="text", text="I will read the file."),
                FakeBlock(type="tool_use", id="tool-1", name="read_file", input={"path": "README.md"}),
            ]
        )


class FakeClient:
    # 暴露与 Anthropic 异步客户端相同的 messages 接口。
    def __init__(self) -> None:
        self.messages = FakeMessages()


# 功能：验证 AnthropicProvider 发出 LLM 事件并解析工具调用。
# 设计：注入最小 SDK 替身，验证请求内容、工具块转换和显式调用耗时，不访问外部模型服务。
def test_anthropic_provider_emits_timed_response_and_tool_call() -> None:
    events: list[AgentEvent] = []
    event_bus = EventBus()
    event_bus.subscribe(events.append)
    client = FakeClient()
    provider = AnthropicProvider(
        LlmConfig(api_key="test-key", default_model="test-model"),
        event_bus,
        [{"name": "read_file", "input_schema": {"type": "object"}}],
        client=client,
    )
    response = asyncio.run(provider.complete("run-1", 1, [{"role": "user", "content": "Read README.md"}]))
    assert client.messages.request["model"] == "test-model"
    assert response.tool_calls[0].name == "read_file"
    assert events[0].type == "llm_request"
    assert events[1].type == "llm_response"
    assert events[1].duration_ms >= 0
