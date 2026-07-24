from dataclasses import dataclass
import time
from typing import Any, Protocol, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from manius_code.core.bus.events import LlmRequestEvent, LlmResponseEvent
from manius_code.core.events.bus import EventBus
from manius_code.core.tracing import TracingProvider

_StructuredModel = TypeVar("_StructuredModel", bound=BaseModel)


@dataclass(frozen=True)
class InstructorRequest:
    messages: list[dict[str, Any]]
    options: dict[str, Any]


class InstructorAdapter(Protocol):
    # 将厂商原生客户端包装成 Instructor 的异步客户端。
    def create_client(self, client: Any) -> Any: ...

    # 将通用消息和系统指令映射为厂商原生请求参数。
    def build_request(
        self,
        messages: list[dict[str, Any]],
        system_instruction: str | None,
    ) -> InstructorRequest: ...


class InstructorStructuredOutput:
    # 注入事件总线、追踪器和厂商请求适配器以统一结构化调用。
    def __init__(
        self,
        event_bus: EventBus,
        adapter: InstructorAdapter,
        tracer: TracingProvider | None = None,
        max_retries: int = 2,
    ) -> None:
        self._event_bus = event_bus
        self._adapter = adapter
        self._tracer = tracer
        self._max_retries = max_retries
        self._raw_client: Any | None = None
        self._instructor_client: Any | None = None

    # 使用 Instructor 的原生 Schema 约束生成并验证强类型结果。
    async def complete(
        self,
        run_id: str,
        step: int,
        raw_client: Any,
        messages: list[dict[str, Any]],
        response_model: type[_StructuredModel],
        system_instruction: str | None = None,
    ) -> _StructuredModel:
        await self._event_bus.publish(LlmRequestEvent(run_id=run_id, step=step, messages=messages))
        started_at = time.monotonic()
        trace_id = uuid4().hex
        request = self._adapter.build_request(messages, system_instruction)
        if self._tracer is not None:
            self._tracer.emit(
                "CORE>LLM",
                "llm",
                "request",
                {
                    "request": {"messages": request.messages, **request.options},
                    "response_model": response_model.__name__,
                    "max_retries": self._max_retries,
                    "message_count": len(request.messages),
                    "tool_count": 0,
                },
                run_id=run_id,
                step=step,
                trace_id=trace_id,
            )
        instructor_client = self._client_for(raw_client)
        response = await instructor_client.create(
            response_model=response_model,
            messages=request.messages,
            max_retries=self._max_retries,
            strict=True,
            **request.options,
        )
        parsed_output = self._validate(response_model, response)
        if self._tracer is not None:
            self._tracer.emit(
                "LLM>CORE",
                "llm",
                "response",
                {"structured_output": parsed_output.model_dump(mode="json")},
                run_id=run_id,
                step=step,
                trace_id=trace_id,
            )
        await self._event_bus.publish(
            LlmResponseEvent(
                run_id=run_id,
                step=step,
                duration_ms=round((time.monotonic() - started_at) * 1000),
                text="",
                tool_calls=[],
            )
        )
        return parsed_output

    # 在原生客户端变更时重建对应的 Instructor 包装客户端。
    def _client_for(self, raw_client: Any) -> Any:
        if raw_client is not self._raw_client:
            self._raw_client = raw_client
            self._instructor_client = self._adapter.create_client(raw_client)
        return self._instructor_client

    # 对 Instructor 返回结果执行最终 Pydantic 校验并给出统一异常。
    def _validate(
        self,
        response_model: type[_StructuredModel],
        payload: Any,
    ) -> _StructuredModel:
        if isinstance(payload, response_model):
            return payload
        try:
            return response_model.model_validate(payload)
        except ValidationError as error:
            raise RuntimeError(
                f"Instructor returned invalid {response_model.__name__} structured output: {error}"
            ) from error
