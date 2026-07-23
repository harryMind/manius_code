import asyncio
import json
from typing import Any

from pydantic import BaseModel

from manius_code.core.autonomy.contracts import PlanProposal, PlanStep
from manius_code.core.autonomy.planner import StructuredAutonomyProvider
from manius_code.core.config import ManiusConfig
from manius_code.core.prompt import plan_instruction
from manius_code.core.tools.defaults import default_tool_catalog


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
    provider = StructuredAutonomyProvider(llm, default_tool_catalog(ManiusConfig()).argument_models())

    plan = asyncio.run(provider.plan("run-1", 0, "Read README", [], ["read_file"]))

    request = llm.requests[0]
    payload = json.loads(request["messages"][0]["content"])
    assert plan.steps[0].allowed_tools == ["read_file"]
    assert payload["available_tools"] == ["read_file"]
    assert "schema" not in payload
    assert request["system_instruction"] == plan_instruction()
    assert request["response_model"] is PlanProposal


# 功能：验证动作规划会按当前步骤白名单生成包含具体工具参数模型的原生响应 schema。
# 设计：只允许 read_file 并检查动态响应模型与最终 ActionProposal，确保业务层只投影 Provider 已校验的结果而不重复校验原始载荷。
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
    provider = StructuredAutonomyProvider(llm, default_tool_catalog(ManiusConfig()).argument_models())

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


# 功能：验证规划响应 Schema 将每步工具和验收列表声明为 API 可见的非空数组。
# 设计：直接检查 Pydantic JSON Schema 的 minItems，而非仅构造对象，确保兼容供应商也能接收硬约束。
def test_plan_proposal_schema_requires_non_empty_tools_and_acceptance_criteria() -> None:
    schema = PlanProposal.model_json_schema()
    step_schema = schema["$defs"]["PlannedStep"]

    assert step_schema["properties"]["allowed_tools"]["minItems"] == 1
    assert step_schema["properties"]["acceptance_criteria"]["minItems"] == 1
