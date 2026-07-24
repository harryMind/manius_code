import time
from typing import Any, Callable, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from manius_code.core.bus.events import LlmRequestEvent, LlmResponseEvent, LlmTokenEvent
from manius_code.core.config import LlmConfig
from manius_code.core.events.bus import EventBus
from manius_code.core.llm.models import LlmResponse, ToolCall
from manius_code.core.llm.structured import InstructorAdapter, InstructorRequest, InstructorStructuredOutput
from manius_code.core.prompt import legacy_agent_instruction
from manius_code.core.tracing import TracingProvider

_StructuredModel = TypeVar("_StructuredModel", bound=BaseModel)


class AnthropicInstructorAdapter:
    # 注入模型配置和可替换的 Instructor 工厂以适配 Anthropic 原生客户端。
    def __init__(
        self,
        config: LlmConfig,
        instructor_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self._config = config
        self._instructor_factory = instructor_factory

    # 将 Anthropic 异步客户端包装为 Instructor 异步客户端。
    def create_client(self, client: Any) -> Any:
        if self._instructor_factory is not None:
            return self._instructor_factory(client)
        import instructor

        return instructor.from_anthropic(client)

    # 构造 Anthropic 消息接口所需的模型、令牌数和系统指令参数。
    def build_request(
        self,
        messages: list[dict[str, Any]],
        system_instruction: str | None,
    ) -> InstructorRequest:
        return InstructorRequest(
            messages=messages,
            options={
                "model": self._config.default_model,
                "max_tokens": 4096,
                "system": system_instruction or legacy_agent_instruction(),
            },
        )


class AnthropicProvider:
    # 注入 Claude 配置、事件总线、工具定义和可替换的 SDK 客户端。
    def __init__(
        self,
        config: LlmConfig,
        event_bus: EventBus,
        tool_definitions: list[dict[str, Any]],
        client: Any | None = None,
        tracer: TracingProvider | None = None,
        structured_adapter: InstructorAdapter | None = None,
    ) -> None:
        self._config = config
        self._event_bus = event_bus
        self._tool_definitions = tool_definitions
        self._client = client
        self._tracer = tracer
        self._structured_output = InstructorStructuredOutput(
            event_bus,
            structured_adapter or AnthropicInstructorAdapter(config),
            tracer=tracer,
        )

    # 向 Anthropic 发送流式上下文并转换为 Agent 可处理的响应结构。
    async def complete(
        self,
        run_id: str,
        step: int,
        messages: list[dict[str, Any]],
        system_instruction: str | None = None,
        emit_tokens: bool = True,
    ) -> LlmResponse:
        await self._event_bus.publish(LlmRequestEvent(run_id=run_id, step=step, messages=messages))
        client = self._client or self._create_client()
        self._client = client
        started_at = time.monotonic()
        trace_id = uuid4().hex
        request_payload = {
            "model": self._config.default_model,
            "max_tokens": 4096,
            "system": system_instruction or legacy_agent_instruction(),
            "messages": messages,
            "tools": self._tool_definitions,
            "cache_control": {"type": "ephemeral"},
        }
        if self._tracer is not None:
            self._tracer.emit(
                "CORE>LLM",
                "llm",
                "request",
                {
                    "request": request_payload,
                    "message_count": len(messages),
                    "tool_count": len(self._tool_definitions),
                },
                run_id=run_id,
                step=step,
                trace_id=trace_id,
            )
        async with client.messages.stream(**request_payload) as stream:
            async for token in stream.text_stream:
                if emit_tokens:
                    await self._event_bus.publish(LlmTokenEvent(run_id=run_id, step=step, token=token))
            response = await stream.get_final_message()
        if self._tracer is not None:
            response_payload = self._response_payload(response)
            self._tracer.emit(
                "LLM>CORE",
                "llm",
                "response",
                {
                    "response": response_payload,
                    "usage": response_payload.get("usage", {}),
                    "content_block_count": len(response_payload.get("content", [])),
                },
                run_id=run_id,
                step=step,
                trace_id=trace_id,
            )
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        assistant_content: list[dict[str, Any]] = []
        for block in response.content:
            block_data = block.model_dump()
            assistant_content.append(block_data)
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=block.input))
        result = LlmResponse(text="\n".join(text_parts), tool_calls=tool_calls, assistant_content=assistant_content)
        await self._event_bus.publish(
            LlmResponseEvent(
                run_id=run_id,
                step=step,
                duration_ms=round((time.monotonic() - started_at) * 1000),
                text=result.text,
                tool_calls=[tool_call.model_dump() for tool_call in result.tool_calls],
            )
        )
        return result

    # 委托通用 Instructor 适配层对 Anthropic 响应执行原生结构化输出。
    async def complete_structured(
        self,
        run_id: str,
        step: int,
        messages: list[dict[str, Any]],
        response_model: type[_StructuredModel],
        system_instruction: str | None = None,
    ) -> _StructuredModel:
        client = self._client or self._create_client()
        self._client = client
        return await self._structured_output.complete(
            run_id,
            step,
            client,
            messages,
            response_model,
            system_instruction,
        )

    # 根据配置惰性创建 Anthropic 异步客户端。
    def _create_client(self) -> Any:
        if not self._config.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required for manius run")
        from anthropic import AsyncAnthropic

        return AsyncAnthropic(
            api_key=self._config.api_key,
            base_url=self._config.default_base_url,
            default_headers={"Authorization": f"Bearer {self._config.api_key}"},
        )

    # 将 SDK 最终消息规范化为可写入 JSON 追踪文件的字典。
    def _response_payload(self, response: Any) -> dict[str, Any]:
        if isinstance(response, BaseModel):
            return response.model_dump(mode="json")
        model_dump = getattr(response, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump(mode="json")
            if isinstance(dumped, dict):
                return dumped
        return {"raw": str(response)}
