from typing import Any, Protocol

from manius_code.core.llm.models import LlmResponse


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
