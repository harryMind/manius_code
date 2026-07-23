from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from manius_code.core.llm.models import LlmResponse

_StructuredModel = TypeVar("_StructuredModel", bound=BaseModel)

class LlmProvider(Protocol):
    # 以统一消息、系统指令和流式开关完成一次跨厂商的模型请求。
    async def complete(
        self,
        run_id: str,
        step: int,
        messages: list[dict[str, Any]],
        system_instruction: str | None = None,
        emit_tokens: bool = True,
    ) -> LlmResponse: ...

    # 由各厂商 Provider 映射原生参数并校验响应后返回与业务模型一致的结构化结果。
    async def complete_structured(
        self,
        run_id: str,
        step: int,
        messages: list[dict[str, Any]],
        response_model: type[_StructuredModel],
        system_instruction: str | None = None,
    ) -> _StructuredModel: ...
