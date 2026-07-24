import asyncio
from collections.abc import AsyncIterator
from io import StringIO
from typing import Any

import pytest
from pydantic import BaseModel

from manius_code.core.autonomy.contracts import PlanProposal
from manius_code.core.bus.events import AgentEvent, LlmTokenEvent
from manius_code.core.config import LlmConfig
from manius_code.core.events.bus import EventBus
from manius_code.core.events.subscribers import StdoutPrinter
from manius_code.core.llm.anthropic import AnthropicProvider
from manius_code.core.llm.structured import InstructorRequest
from manius_code.core.prompt import legacy_agent_instruction


class FakeBlock(BaseModel):
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] = {}


class FakeResponse(BaseModel):
    content: list[FakeBlock]


class FakeStream:
    # 保存最终响应并提供与 Anthropic SDK 一致的异步流接口。
    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.text_stream = self._tokens()

    # 进入模拟流式上下文。
    async def __aenter__(self) -> "FakeStream":
        return self

    # 退出模拟流式上下文。
    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        return None

    # 逐段产生模型文本以模拟 token 到达。
    async def _tokens(self) -> AsyncIterator[str]:
        yield "I will "
        yield "read the file."

    # 返回流结束后的完整消息供工具调用解析。
    async def get_final_message(self) -> FakeResponse:
        return self._response


class FakeMessages:
    # 保存流式请求并返回确定性的 Claude 内容块。
    def __init__(self) -> None:
        self.request: dict[str, Any] | None = None

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


class FakeClient:
    # 暴露 Anthropic 异步客户端所需的 messages 接口。
    def __init__(self) -> None:
        self.messages = FakeMessages()


class FakeInstructorClient:
    # 注入可选响应载荷以验证 Instructor 调用和最终 Pydantic 校验。
    def __init__(self, response_payload: Any | None = None) -> None:
        self._response_payload = response_payload
        self.request: dict[str, Any] | None = None

    # 记录 Instructor 结构化请求并返回约定的模型载荷。
    async def create(self, **kwargs: Any) -> Any:
        self.request = kwargs
        if self._response_payload is not None:
            return self._response_payload
        response_model = kwargs["response_model"]
        return response_model.model_validate(
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
        )


class FakeInstructorAdapter:
    # 持有确定性 Instructor 客户端并统计原生客户端包装次数。
    def __init__(self, instructor_client: FakeInstructorClient) -> None:
        self._instructor_client = instructor_client
        self.raw_clients: list[Any] = []

    # 返回预置 Instructor 客户端以避免测试访问真实 SDK。
    def create_client(self, client: Any) -> FakeInstructorClient:
        self.raw_clients.append(client)
        return self._instructor_client

    # 映射 Anthropic 请求字段以验证通用层不关心厂商 SDK。
    def build_request(
        self,
        messages: list[dict[str, Any]],
        system_instruction: str | None,
    ) -> InstructorRequest:
        return InstructorRequest(
            messages=messages,
            options={
                "model": "test-model",
                "max_tokens": 4096,
                "system": system_instruction or legacy_agent_instruction(),
            },
        )


# 功能：验证 AnthropicProvider 发送流式事件并解析工具调用。
# 设计：注入最小 SDK 替身，覆盖文本流、工具块转换和事件顺序而不访问外部模型服务。
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


# 功能：验证结构化请求由 Instructor 统一接收 Pydantic 响应模型和严格重试参数。
# 设计：用独立 Instructor 替身断言通用层调用 create，避免测试依赖 Anthropic SDK 的 parse 或工具回退细节。
def test_anthropic_provider_delegates_structured_output_to_instructor() -> None:
    events: list[AgentEvent] = []
    event_bus = EventBus()
    event_bus.subscribe(events.append)
    client = FakeClient()
    instructor_client = FakeInstructorClient()
    adapter = FakeInstructorAdapter(instructor_client)
    provider = AnthropicProvider(
        LlmConfig(api_key="test-key", default_model="test-model"),
        event_bus,
        [],
        client=client,
        structured_adapter=adapter,
    )

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
    assert adapter.raw_clients == [client]
    assert instructor_client.request["response_model"] is PlanProposal
    assert instructor_client.request["max_retries"] == 2
    assert instructor_client.request["strict"] is True
    assert instructor_client.request["model"] == "test-model"
    assert instructor_client.request["system"] == "Return a plan"
    assert [event.type for event in events] == ["llm_request", "llm_response"]


# 功能：验证 Instructor 返回不符合 Pydantic Schema 的载荷时不会进入自主规划层。
# 设计：替身绕过 Instructor 内部重试直接返回缺失字段的字典，覆盖通用层最后一道模型校验边界。
def test_anthropic_provider_rejects_invalid_instructor_structured_output() -> None:
    event_bus = EventBus()
    provider = AnthropicProvider(
        LlmConfig(api_key="test-key", default_model="test-model"),
        event_bus,
        [],
        client=FakeClient(),
        structured_adapter=FakeInstructorAdapter(FakeInstructorClient({"steps": []})),
    )

    with pytest.raises(RuntimeError, match="Instructor returned invalid PlanProposal structured output"):
        asyncio.run(
            provider.complete_structured(
                "run-structured",
                2,
                [{"role": "user", "content": "Create a plan"}],
                PlanProposal,
            )
        )


# 功能：验证同一 Anthropic 原生客户端只会创建一次 Instructor 包装客户端。
# 设计：连续发起两次结构化请求，断言适配器工厂只调用一次以避免重复补丁和连接状态污染。
def test_anthropic_provider_reuses_instructor_client_for_same_raw_client() -> None:
    event_bus = EventBus()
    client = FakeClient()
    adapter = FakeInstructorAdapter(FakeInstructorClient())
    provider = AnthropicProvider(
        LlmConfig(api_key="test-key", default_model="test-model"),
        event_bus,
        [],
        client=client,
        structured_adapter=adapter,
    )

    asyncio.run(provider.complete_structured("run-1", 1, [{"role": "user", "content": "Plan"}], PlanProposal))
    asyncio.run(provider.complete_structured("run-2", 1, [{"role": "user", "content": "Plan"}], PlanProposal))

    assert adapter.raw_clients == [client]


# 功能：验证终端订阅器收到 token 事件后立即原样输出且不追加换行。
# 设计：使用内存文本流观察精确输出，避免依赖真实终端缓冲策略。
def test_stdout_printer_writes_each_llm_token_without_newline() -> None:
    stream = StringIO()
    printer = StdoutPrinter(stream)
    printer.handle(LlmTokenEvent(run_id="run-1", step=1, token="streamed text"))

    assert stream.getvalue() == "streamed text"
