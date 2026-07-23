import asyncio
import json
from typing import Any

from manius_code.core.autonomy.planner import StructuredAutonomyProvider
from manius_code.core.llm.models import LlmResponse
from manius_code.core.prompt import plan_instruction


class FakeLlmProvider:
    # 保存通用 LLM 调用参数并返回预设的结构化文本响应。
    def __init__(self, text: str) -> None:
        self._text = text
        self.requests: list[dict[str, Any]] = []

    # 实现 LlmProvider 契约而不依赖任何特定厂商 SDK。
    async def complete(
        self,
        run_id: str,
        step: int,
        messages: list[dict[str, Any]],
        system_instruction: str | None = None,
        emit_tokens: bool = True,
    ) -> LlmResponse:
        self.requests.append(
            {
                "run_id": run_id,
                "step": step,
                "messages": messages,
                "system_instruction": system_instruction,
                "emit_tokens": emit_tokens,
            }
        )
        return LlmResponse(text=self._text, tool_calls=[], assistant_content=[])


# 功能：验证 StructuredAutonomyProvider 只依赖 LlmProvider 契约即可构造并校验规划结果。
# 设计：使用不导入 Anthropic SDK 的结构替身，检查动态工具清单和结构化请求参数均经通用接口传递。
def test_structured_autonomy_provider_accepts_vendor_neutral_llm_provider() -> None:
    response = {
        "goal": "Read README",
        "steps": [
            {
                "id": "inspect",
                "title": "Read README",
                "allowed_tools": ["read_file"],
                "acceptance_criteria": [{"kind": "tool_result_contains", "expected": "Manius"}],
            }
        ],
    }
    llm = FakeLlmProvider(json.dumps(response))
    provider = StructuredAutonomyProvider(llm)

    plan = asyncio.run(provider.plan("run-1", 0, "Read README", [], ["read_file"]))

    request = llm.requests[0]
    payload = json.loads(request["messages"][0]["content"])
    assert plan.steps[0].allowed_tools == ["read_file"]
    assert payload["available_tools"] == ["read_file"]
    assert request["system_instruction"] == plan_instruction()
    assert request["emit_tokens"] is False
