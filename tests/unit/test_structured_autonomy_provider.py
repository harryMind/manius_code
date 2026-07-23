import asyncio
import json
from typing import Any

from pydantic import BaseModel

from manius_code.core.autonomy.contracts import PlanProposal, PlanStep
from manius_code.core.autonomy.planner import StructuredAutonomyProvider
from manius_code.core.prompt import plan_instruction


class FakeLlmProvider:
    # 保存通用 LLM 调用参数并返回预设的结构化文本响应。
    def __init__(self, text: str) -> None:
        self._text = text
        self.requests: list[dict[str, Any]] = []

    # 若结构化调用错误退回文本接口则立即失败，以覆盖原生输出路径。
    async def complete(
        self,
        run_id: str,
        step: int,
        messages: list[dict[str, Any]],
        system_instruction: str | None = None,
        emit_tokens: bool = True,
    ) -> None:
        raise AssertionError("structured autonomy requests must not use text completion")

    # 接收 Pydantic 响应模型并直接返回已经校验的结构化对象。
    async def complete_structured(
        self,
        run_id: str,
        step: int,
        messages: list[dict[str, Any]],
        response_model: type[BaseModel],
        system_instruction: str | None = None,
    ) -> BaseModel:
        self.requests.append(
            {
                "run_id": run_id,
                "step": step,
                "messages": messages,
                "system_instruction": system_instruction,
                "response_model": response_model,
            }
        )
        payload = json.loads(self._text)
        if "action" in response_model.model_fields:
            payload = {"action": payload}
        return response_model.model_validate(payload)


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
    assert "schema" not in payload
    assert request["system_instruction"] == plan_instruction()
    assert request["response_model"] is PlanProposal


# 功能：验证动作规划会按当前步骤白名单生成包含具体工具参数模型的原生响应 schema。
# 设计：只允许 read_file 并检查动态响应模型与最终 ActionProposal，防止无界 dict 在 API schema 中退化为空对象。
def test_structured_autonomy_provider_uses_tool_specific_action_schema() -> None:
    llm = FakeLlmProvider(
        json.dumps(
            {
                "step_id": "inspect",
                "tool_name": "read_file",
                "arguments": {"path": "README.md"},
                "rationale": "inspect the requested file",
            }
        )
    )
    provider = StructuredAutonomyProvider(llm)

    action = asyncio.run(
        provider.action(
            "run-1",
            1,
            PlanStep(id="inspect", title="Inspect", allowed_tools=["read_file"]),
            [],
        )
    )
    response_model = llm.requests[0]["response_model"]
    schema = response_model.model_json_schema()

    assert action.tool_name == "read_file"
    assert action.arguments == {"path": "README.md"}
    assert "ReadFileArguments" in schema["$defs"]
    assert "WriteFileArguments" not in schema["$defs"]
    assert "$ref" in schema["properties"]["action"]
    assert schema["$defs"]["StructuredAction_read_file"]["properties"]["arguments"] == {
        "$ref": "#/$defs/ReadFileArguments"
    }
