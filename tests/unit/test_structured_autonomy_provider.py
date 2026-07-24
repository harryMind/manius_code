import asyncio
import json
from typing import Any

from pydantic import BaseModel

from manius_code.core.autonomy.contracts import AuditResult, AuditViolation, PlanProposal, PlanStep, StepResult
from manius_code.core.autonomy.planner import StructuredAutonomyProvider
from manius_code.core.config import ManiusConfig
from manius_code.core.prompt import batch_action_instruction, plan_instruction
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


class BatchIncompatibleLlmProvider(FakeLlmProvider):
    # 记录批量结构化请求并模拟兼容端点拒绝复杂批量 Schema 的行为。
    def __init__(self) -> None:
        super().__init__("")
        self.batch_requests = 0
        self.single_requests = 0

    # 对批量响应抛出结构化调用错误，对单动作响应返回可验证的受限动作。
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
        if "actions" in response_model.model_fields:
            self.batch_requests += 1
            raise RuntimeError("required schema tool was not called")
        self.single_requests += 1
        payload = json.loads(messages[0]["content"])
        return response_model.model_validate(
            {
                "action": {
                    "step_id": payload["plan_step"]["id"],
                    "tool_name": "read_file",
                    "arguments": {"path": "README.md"},
                }
            }
        )


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
    assert payload["workspace_root"]
    assert "schema" not in payload
    assert request["system_instruction"] == plan_instruction()
    assert request["response_model"] is PlanProposal


# 功能：验证计划审计失败时仅将最新的结构化违规报告传给下一次规划请求。
# 设计：直接构造一个 AuditResult 并检查独立 payload 字段，避免把审计数据伪装成长期记忆或原始日志。
def test_structured_autonomy_provider_includes_latest_plan_audit_report() -> None:
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
    audit_report = AuditResult(
        approved=False,
        summary="step inspect uses unavailable tools: ['delete_all']",
        violations=[
            AuditViolation(code="unavailable_tool", message="step inspect uses unavailable tools: ['delete_all']")
        ],
    )
    llm = FakeLlmProvider(json.dumps(response))
    provider = StructuredAutonomyProvider(llm, default_tool_catalog(ManiusConfig()).argument_models())

    asyncio.run(provider.plan("run-1", 0, "Read README", [], ["read_file"], audit_report))
    payload = json.loads(llm.requests[0]["messages"][0]["content"])

    assert payload["latest_plan_audit_report"]["violations"] == [
        {"code": "unavailable_tool", "message": "step inspect uses unavailable tools: ['delete_all']"}
    ]
    assert payload["verified_memories"] == []


# 功能：验证动作重试仅携带最近一次动作审计报告且不累积旧审计错误。
# 设计：构造两次审计失败历史并检查 payload 只包含末次违规，普通 attempts 中不再重复嵌入审计报告。
def test_structured_autonomy_provider_includes_only_latest_action_audit_report() -> None:
    llm = FakeLlmProvider(
        json.dumps(
            {
                "step_id": "inspect",
                "tool_name": "read_file",
                "arguments": {"path": "README.md"},
            }
        )
    )
    provider = StructuredAutonomyProvider(llm, default_tool_catalog(ManiusConfig()).argument_models())
    old_report = AuditResult(
        approved=False,
        violations=[AuditViolation(code="old", message="old violation")],
    )
    latest_report = AuditResult(
        approved=False,
        violations=[AuditViolation(code="tool_not_allowed", message="use read_file")],
    )
    history = [
        StepResult(step_id="inspect", attempt=1, error="old violation", audit_report=old_report),
        StepResult(step_id="inspect", attempt=2, error="use read_file", audit_report=latest_report),
        StepResult(step_id="other", attempt=1, observation="another parallel step completed"),
    ]

    asyncio.run(
        provider.action(
            "run-1",
            2,
            PlanStep(id="inspect", title="Inspect", allowed_tools=["read_file"]),
            history,
        )
    )
    payload = json.loads(llm.requests[0]["messages"][0]["content"])

    assert payload["latest_action_audit_report"]["violations"] == [
        {"code": "tool_not_allowed", "message": "use read_file"}
    ]
    assert [attempt["step_id"] for attempt in payload["recent_attempts"]] == ["other"]
    assert all(attempt["audit_report"] is None for attempt in payload["recent_attempts"])


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


# 功能：验证支持批量能力的结构化 Provider 会在一次原生请求中返回当前滚动批次的全部原子动作。
# 设计：为两个同工具步骤提供单个响应包络，并同时断言输入负载和动态 Schema 的步骤约束。
def test_structured_autonomy_provider_uses_one_structured_request_for_a_rolling_batch() -> None:
    llm = FakeLlmProvider(
        json.dumps(
            {
                "actions": [
                    {"step_id": "first", "tool_name": "read_file", "arguments": {"path": "README.md"}},
                    {"step_id": "second", "tool_name": "read_file", "arguments": {"path": "README.md"}},
                ]
            }
        )
    )
    provider = StructuredAutonomyProvider(llm, default_tool_catalog(ManiusConfig()).argument_models())
    plan_steps = [
        PlanStep(id="first", title="First", allowed_tools=["read_file"]),
        PlanStep(id="second", title="Second", allowed_tools=["read_file"]),
    ]

    actions = asyncio.run(provider.actions("run-1", 1, plan_steps, []))
    request = llm.requests[0]
    payload = json.loads(request["messages"][0]["content"])

    assert [action.step_id for action in actions] == ["first", "second"]
    assert [step["id"] for step in payload["plan_steps"]] == ["first", "second"]
    assert request["system_instruction"] == batch_action_instruction()
    assert request["response_model"].model_fields["actions"].annotation


# 功能：验证兼容端点拒绝批量结构化 Schema 时会自动降级为同批步骤的单动作结构化调用。
# 设计：首个批量请求固定抛出真实日志中的 schema-tool 错误，再断言后续请求只使用已验证的单动作 Schema。
def test_structured_autonomy_provider_falls_back_to_single_actions_for_batch_schema_incompatibility() -> None:
    llm = BatchIncompatibleLlmProvider()
    provider = StructuredAutonomyProvider(llm, default_tool_catalog(ManiusConfig()).argument_models())
    plan_steps = [
        PlanStep(id="first", title="First", allowed_tools=["read_file"]),
        PlanStep(id="second", title="Second", allowed_tools=["read_file"]),
    ]

    actions = asyncio.run(provider.actions("run-1", 1, plan_steps, []))
    repeated_actions = asyncio.run(provider.actions("run-1", 2, plan_steps, []))

    assert [action.step_id for action in actions] == ["first", "second"]
    assert [action.step_id for action in repeated_actions] == ["first", "second"]
    assert llm.batch_requests == 1
    assert llm.single_requests == 4


# 功能：验证规划响应 Schema 将每步工具和验收列表声明为 API 可见的非空数组。
# 设计：直接检查 Pydantic JSON Schema 的 minItems，而非仅构造对象，确保兼容供应商也能接收硬约束。
def test_plan_proposal_schema_requires_non_empty_tools_and_acceptance_criteria() -> None:
    schema = PlanProposal.model_json_schema()
    step_schema = schema["$defs"]["PlannedStep"]

    assert step_schema["properties"]["allowed_tools"]["minItems"] == 1
    assert step_schema["properties"]["allowed_tools"]["maxItems"] == 1
    assert step_schema["properties"]["acceptance_criteria"]["minItems"] == 1
    assert step_schema["properties"]["artifacts"]["maxItems"] == 1
