import asyncio
from collections.abc import AsyncIterator
from io import StringIO
from typing import Any

from pydantic import BaseModel

from manius_code.core.autonomy.contracts import PlanProposal
from manius_code.core.config import LlmConfig
from manius_code.core.events.bus import EventBus
from manius_code.core.bus.events import AgentEvent, LlmTokenEvent
from manius_code.core.events.subscribers import StdoutPrinter
from manius_code.core.llm.anthropic import AnthropicProvider
from manius_code.core.prompt import legacy_agent_instruction


class FakeBlock(BaseModel):
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] = {}


class FakeResponse(BaseModel):
    content: list[FakeBlock]


class FakeParsedResponse(FakeResponse):
    parsed_output: Any = None


class FakeStream:
    # 模拟 SDK 流式上下文管理器并依次提供文本片段。
    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.text_stream = self._tokens()

    # 进入模拟流式上下文。
    async def __aenter__(self) -> "FakeStream":
        return self

    # 退出模拟流式上下文。
    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        return None

    # 逐段生成模型文本以模拟 token 到达。
    async def _tokens(self) -> AsyncIterator[str]:
        yield "I will "
        yield "read the file."

    # 返回流结束后的完整消息供工具调用解析。
    async def get_final_message(self) -> FakeResponse:
        return self._response


class FakeMessages:
    # 保存请求参数并返回确定性的 Claude 内容块。
    def __init__(self) -> None:
        self.request: dict[str, Any] | None = None
        self.structured_request: dict[str, Any] | None = None

    # 模拟 Anthropic messages.stream 并保存请求参数。
    def stream(self, **kwargs: Any) -> FakeStream:
        self.request = kwargs
        return FakeStream(
            FakeResponse(
                content=[
                    FakeBlock(type="text", text="I will read the file."),
                    FakeBlock(type="tool_use", id="tool-1", name="read_file", input={"path": "README.md"}),
                ]
            )
        )

    # 模拟 SDK parse 接口接收 Pydantic 模型并返回已校验的 parsed_output。
    async def parse(self, **kwargs: Any) -> FakeParsedResponse:
        self.structured_request = kwargs
        response_model = kwargs["output_format"]
        return FakeParsedResponse(
            content=[FakeBlock(type="text", text='{"goal":"Structured"}')],
            parsed_output=response_model.model_validate(
                {
                    "goal": "Structured",
                    "steps": [
                        {
                            "id": "inspect",
                            "title": "Inspect",
                            "allowed_tools": ["read_file"],
                            "acceptance_criteria": [{"kind": "tool_result_contains", "expected": "ok"}],
                        }
                    ],
                }
            ),
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
    assert client.messages.request["system"] == legacy_agent_instruction()
    assert response.tool_calls[0].name == "read_file"
    assert events[0].type == "llm_request"
    assert [event.token for event in events if event.type == "llm_token"] == ["I will ", "read the file."]
    assert events[-1].type == "llm_response"
    assert events[-1].duration_ms >= 0


# 功能：验证 AnthropicProvider 将 Pydantic 模型直接传给原生 messages.parse 并返回 parsed_output。
# 设计：以 SDK 替身检查 output_format 参数对象身份，同时断言结构化路径不产生逐 token 事件或文本 JSON 解析依赖。
def test_anthropic_provider_uses_native_pydantic_structured_output() -> None:
    events: list[AgentEvent] = []
    event_bus = EventBus()
    event_bus.subscribe(events.append)
    client = FakeClient()
    provider = AnthropicProvider(LlmConfig(api_key="test-key", default_model="test-model"), event_bus, [], client=client)

    plan = asyncio.run(
        provider.complete_structured(
            "run-structured",
            2,
            [{"role": "user", "content": "Create a plan"}],
            PlanProposal,
            system_instruction="Return a plan",
        )
    )

    assert plan.goal == "Structured"
    assert client.messages.structured_request["output_format"] is PlanProposal
    assert client.messages.structured_request["system"] == "Return a plan"
    assert [event.type for event in events] == ["llm_request", "llm_response"]


# 功能：验证终端订阅器收到 token 事件后立即原样输出且不追加换行。
# 设计：使用内存文本流观察精确输出，避免依赖真实终端缓冲策略。
def test_stdout_printer_writes_each_llm_token_without_newline() -> None:
    stream = StringIO()
    printer = StdoutPrinter(stream)
    printer.handle(LlmTokenEvent(run_id="run-1", step=1, token="streamed text"))

    assert stream.getvalue() == "streamed text"
