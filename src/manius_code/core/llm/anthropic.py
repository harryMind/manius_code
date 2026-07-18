import time
from typing import Any

from pydantic import BaseModel

from manius_code.core.config import LlmConfig
from manius_code.core.events.bus import EventBus
from manius_code.core.bus.events import LlmRequestEvent, LlmResponseEvent, LlmTokenEvent


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class LlmResponse(BaseModel):
    text: str
    tool_calls: list[ToolCall]
    assistant_content: list[dict[str, Any]]


class AnthropicProvider:
    # 注入 Claude 配置、事件总线和可选的异步 SDK 客户端。
    def __init__(self, config: LlmConfig, event_bus: EventBus, tool_definitions: list[dict[str, Any]], client: Any | None = None) -> None:
        self._config = config
        self._event_bus = event_bus
        self._tool_definitions = tool_definitions
        self._client = client

    # 向 Anthropic 发送上下文并将响应转换为 Agent 可处理结构。
    async def complete(self, run_id: str, step: int, messages: list[dict[str, Any]]) -> LlmResponse:
        await self._event_bus.publish(LlmRequestEvent(run_id=run_id, step=step, messages=messages))
        client = self._client or self._create_client()
        self._client = client
        started_at = time.monotonic()
        async with client.messages.stream(
            model=self._config.default_model,
            max_tokens=4096,
            system="""
                        "You are a helpful AI assistant. "
                        "Use the available tools to complete the user's goal. "
                        "When the goal is fully achieved, respond with a final answer "
                        "and do not call any more tools."
            """,
            messages=messages,
            tools=self._tool_definitions,
        ) as stream:
            async for token in stream.text_stream:
                await self._event_bus.publish(LlmTokenEvent(run_id=run_id, step=step, token=token))
            response = await stream.get_final_message()
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
