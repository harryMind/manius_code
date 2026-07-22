from __future__ import annotations

import json
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from manius_code.core.autonomy.contracts import ActionProposal, PlanProposal, PlanStep, ResolverDecision, StepResult
from manius_code.core.llm.anthropic import AnthropicProvider, LlmResponse

_Model = TypeVar("_Model", bound=BaseModel)


class AutonomyProvider(Protocol):
    # 为目标和记忆提出一份可审计的结构化计划。
    async def plan(
        self,
        run_id: str,
        step: int,
        goal: str,
        memories: list[str],
        available_tools: list[str],
    ) -> PlanProposal: ...

    # 为已经调度的计划步骤提出一个受限工具动作。
    async def action(self, run_id: str, step: int, plan_step: PlanStep, history: list[StepResult]) -> ActionProposal: ...

    # 根据失败事实提出重试、修订、重规划或中止决策。
    async def resolve(
        self,
        run_id: str,
        step: int,
        goal: str,
        plan_step: PlanStep,
        result: StepResult,
        history: list[StepResult],
    ) -> ResolverDecision: ...

    # 汇总所有已验证步骤并产出面向用户的最终结果。
    async def summarize(self, run_id: str, step: int, goal: str, plan: PlanProposal, history: list[StepResult]) -> str: ...


class StructuredAutonomyProvider:
    # 注入复用的 Anthropic Provider，所有新运行时请求均禁用旧工具调用协议。
    def __init__(self, provider: AnthropicProvider) -> None:
        self._provider = provider

    # 请求模型仅返回符合 PlanProposal schema 的初始计划。
    async def plan(
        self,
        run_id: str,
        step: int,
        goal: str,
        memories: list[str],
        available_tools: list[str],
    ) -> PlanProposal:
        return await self._request(
            run_id,
            step,
            "You are the Planner in a deterministic agent runtime. Return JSON only, with no markdown and no tool calls. "
            "Create the smallest dependency DAG that can achieve the goal. The available_tools field is the complete tool "
            "allowlist: use only its exact values and never invent aliases such as filesystem_read or filesystem_write. Each "
            "step must declare allowed_tools and at least one verifiable acceptance_criteria using file_exists, file_contains, "
            "or tool_result_contains.",
            {
                "goal": goal,
                "verified_memories": memories,
                "available_tools": available_tools,
                "schema": PlanProposal.model_json_schema(),
            },
            PlanProposal,
        )

    # 请求模型仅返回当前步骤允许的一个 ActionProposal。
    async def action(self, run_id: str, step: int, plan_step: PlanStep, history: list[StepResult]) -> ActionProposal:
        return await self._request(
            run_id,
            step,
            "You are the Executor planner. Return JSON only, with no markdown and no tool calls. Propose exactly one action "
            "for the supplied step. Its tool_name must be in allowed_tools and all paths must be workspace-relative.",
            {
                "plan_step": plan_step.model_dump(mode="json"),
                "recent_attempts": [item.model_dump(mode="json") for item in history[-6:]],
                "schema": ActionProposal.model_json_schema(),
            },
            ActionProposal,
        )

    # 请求模型在失败后返回受限的 ResolverDecision。
    async def resolve(
        self,
        run_id: str,
        step: int,
        goal: str,
        plan_step: PlanStep,
        result: StepResult,
        history: list[StepResult],
    ) -> ResolverDecision:
        return await self._request(
            run_id,
            step,
            "You are the Resolver. Return JSON only, with no markdown and no tool calls. Use retry for a transient or correctable "
            "action failure, revise_step when the existing step needs correction, replan when dependencies are invalid, and abort only "
            "when the goal cannot be safely completed. A replan decision must include a complete PlanProposal.",
            {
                "goal": goal,
                "plan_step": plan_step.model_dump(mode="json"),
                "failure": result.model_dump(mode="json"),
                "recent_attempts": [item.model_dump(mode="json") for item in history[-6:]],
                "schema": ResolverDecision.model_json_schema(),
            },
            ResolverDecision,
        )

    # 请求模型基于验证过的执行事实生成最终摘要。
    async def summarize(self, run_id: str, step: int, goal: str, plan: PlanProposal, history: list[StepResult]) -> str:
        response = await self._provider.complete(
            run_id,
            step,
            [
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "goal": goal,
                            "plan": plan.model_dump(mode="json"),
                            "verified_attempts": [item.model_dump(mode="json") for item in history],
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
            system_instruction="You summarize a completed agent run. Use only the supplied verified facts and return a concise user-facing result.",
            emit_tokens=True,
        )
        return response.text.strip()

    # 通过统一的 JSON 提示、解析和 Pydantic 校验调用底层模型。
    async def _request(
        self,
        run_id: str,
        step: int,
        instruction: str,
        payload: dict[str, object],
        model: type[_Model],
    ) -> _Model:
        response = await self._provider.complete(
            run_id,
            step,
            [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            system_instruction=instruction,
            emit_tokens=False,
        )
        return self._parse_response(response, model)

    # 将模型文本中的 JSON 转换为调用方要求的 Pydantic 契约。
    def _parse_response(self, response: LlmResponse, model: type[_Model]) -> _Model:
        content = response.text.strip()
        if content.startswith("```") and content.endswith("```"):
            content = content.split("\n", 1)[1].rsplit("\n", 1)[0]
        try:
            return model.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as error:
            raise RuntimeError(f"LLM returned invalid {model.__name__} JSON: {error}") from error


class Planner:
    # 注入满足规划契约的模型适配器以隔离计划生成职责。
    def __init__(self, provider: AutonomyProvider) -> None:
        self._provider = provider

    # 委托模型提出首个可进入审计流程的计划。
    async def create(
        self,
        run_id: str,
        step: int,
        goal: str,
        memories: list[str],
        available_tools: list[str],
    ) -> PlanProposal:
        return await self._provider.plan(run_id, step, goal, memories, available_tools)

    # 委托模型针对一个已就绪步骤提出受限动作。
    async def propose_action(
        self,
        run_id: str,
        step: int,
        plan_step: PlanStep,
        history: list[StepResult],
    ) -> ActionProposal:
        return await self._provider.action(run_id, step, plan_step, history)
