import time
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from manius_code.core.config import LlmConfig
from manius_code.core.events.bus import EventBus
from manius_code.core.bus.events import LlmRequestEvent, LlmResponseEvent, LlmTokenEvent
from manius_code.core.llm.models import LlmResponse, ToolCall
from manius_code.core.prompt import legacy_agent_instruction
from manius_code.core.tracing import TracingProvider

_StructuredModel = TypeVar("_StructuredModel", bound=BaseModel)

class AnthropicProvider:
    # 注入 Claude 配置、事件总线和可选的异步 SDK 客户端。
    def __init__(
        self,
        config: LlmConfig,
        event_bus: EventBus,
        tool_definitions: list[dict[str, Any]],
        client: Any | None = None,
        tracer: TracingProvider | None = None,
    ) -> None:
        self._config = config
        self._event_bus = event_bus
        self._tool_definitions = tool_definitions
        self._client = client
        self._tracer = tracer

    # 向 Anthropic 发送上下文并将响应转换为 Agent 可处理结构。
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

    # 直接将 Pydantic 响应模型交给 Anthropic 原生 parse 接口并返回 API 约束后的结果。
    async def complete_structured(
        self,
        run_id: str,
        step: int,
        messages: list[dict[str, Any]],
        response_model: type[_StructuredModel],
        system_instruction: str | None = None,
    ) -> _StructuredModel:
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
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": response_model.model_json_schema(),
                }
            },
        }
        if self._tracer is not None:
            self._tracer.emit(
                "CORE>LLM",
                "llm",
                "request",
                {
                    "request": request_payload,
                    "message_count": len(messages),
                    "tool_count": 0,
                },
                run_id=run_id,
                step=step,
                trace_id=trace_id,
            )
        try:
            response = await client.messages.parse(
                model=self._config.default_model,
                max_tokens=4096,
                system=system_instruction or legacy_agent_instruction(),
                messages=messages,
                output_format=response_model,
            )
            parsed_output = response.parsed_output
            if not isinstance(parsed_output, response_model):
                response, parsed_output = await self._complete_with_schema_tool(
                    client,
                    messages,
                    response_model,
                    system_instruction or legacy_agent_instruction(),
                    run_id,
                    step,
                    trace_id,
                )
        except ValidationError:
            response, parsed_output = await self._complete_with_schema_tool(
                client,
                messages,
                response_model,
                system_instruction or legacy_agent_instruction(),
                run_id,
                step,
                trace_id,
            )
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
        text = "\n".join(block.text for block in response.content if block.type == "text")
        await self._event_bus.publish(
            LlmResponseEvent(
                run_id=run_id,
                step=step,
                duration_ms=round((time.monotonic() - started_at) * 1000),
                text=text,
                tool_calls=[],
            )
        )
        return parsed_output

    # 在兼容端点忽略 output_config 时，以强制单一工具调用继续取得受 Schema 约束的结果。
    async def _complete_with_schema_tool(
        self,
        client: Any,
        messages: list[dict[str, Any]],
        response_model: type[_StructuredModel],
        system_instruction: str,
        run_id: str,
        step: int,
        trace_id: str,
    ) -> tuple[Any, _StructuredModel]:
        tool_name = "emit_structured_result"
        request_payload = {
            "model": self._config.default_model,
            "max_tokens": 4096,
            "system": system_instruction,
            "messages": messages,
            "tools": [
                {
                    "name": tool_name,
                    "description": "Return the requested structured result.",
                    "input_schema": response_model.model_json_schema(),
                }
            ],
            "tool_choice": {"type": "tool", "name": tool_name},
        }
        if self._tracer is not None:
            self._tracer.emit(
                "CORE>LLM",
                "llm",
                "request",
                {"request": request_payload, "fallback_for": "output_config", "message_count": len(messages), "tool_count": 1},
                run_id=run_id,
                step=step,
                trace_id=trace_id,
            )
        response = await client.messages.create(**request_payload)
        for block in response.content:
            if block.type != "tool_use" or block.name != tool_name:
                continue
            try:
                return response, response_model.model_validate(block.input)
            except ValidationError as error:
                raise RuntimeError(f"LLM returned invalid {response_model.__name__} schema-tool arguments") from error
        raise RuntimeError(f"LLM did not call required schema tool for {response_model.__name__}")

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

    # 将 SDK 的最终消息完整转换为可写入 JSON 追踪文件的字典。
    def _response_payload(self, response: Any) -> dict[str, Any]:
        if isinstance(response, BaseModel):
            return response.model_dump(mode="json")
        model_dump = getattr(response, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump(mode="json")
            if isinstance(dumped, dict):
                return dumped
        return {"raw": str(response)}
