from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Protocol, TypeVar

from pydantic import BaseModel

from manius_code.core.autonomy.contracts import ActionProposal, PlanProposal, PlanStep, ResolverDecision, StepResult
from manius_code.core.autonomy.structured_models import action_response_model
from manius_code.core.llm.provider import LlmProvider
from manius_code.core.prompt import action_instruction, plan_instruction, resolver_instruction, summary_instruction

_Model = TypeVar("_Model", bound=BaseModel)

# 定义智能体自主运行必须具备四大能力 也就是规划器的核心操作
class AutonomyProvider(Protocol):
    # 为目标和记忆提出一份可审计的结构化计划。 拆分多个PlanStep，约束是依据提示词写死的
    async def plan(
        self,
        run_id: str,
        step: int,
        goal: str,
        memories: list[str],
        available_tools: list[str],
    ) -> PlanProposal: ...

    # 为已经调度的计划步骤提出一个受限工具动作。plan() → PlanProposal[PlanStepA, PlanStepB]
    # 选择一个PlanStepA调用其action
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
    # 注入任意满足通用 LLM 契约的实现，隔离厂商 SDK 与自主规划逻辑。
    def __init__(
        self,
        provider: LlmProvider,
        tool_argument_models: Mapping[str, type[BaseModel]],
        workspace: Path | None = None,
    ) -> None:
        self._provider = provider
        self._tool_argument_models = tool_argument_models
        self._workspace = (workspace or Path.cwd()).expanduser().resolve()

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
            plan_instruction(),
            {
                "goal": goal,
                "workspace_root": str(self._workspace),
                "verified_memories": memories,
                "available_tools": available_tools,
            },
            PlanProposal,
        )

    # 请求模型仅返回当前步骤允许的一个 ActionProposal。
    async def action(self, run_id: str, step: int, plan_step: PlanStep, history: list[StepResult]) -> ActionProposal:
        response = await self._request(
            run_id,
            step,
            action_instruction(),
            {
                "plan_step": plan_step.model_dump(mode="json"),
                "workspace_root": str(self._workspace),
                "recent_attempts": [item.model_dump(mode="json") for item in history[-6:]],
            },
            action_response_model(plan_step.id, plan_step.allowed_tools, self._tool_argument_models),
        )
        return ActionProposal.model_construct(**response.action.model_dump(mode="json"))

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
            resolver_instruction(),
            {
                "goal": goal,
                "workspace_root": str(self._workspace),
                "plan_step": plan_step.model_dump(mode="json"),
                "failure": result.model_dump(mode="json"),
                "recent_attempts": [item.model_dump(mode="json") for item in history[-6:]],
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
            system_instruction=summary_instruction(),
            emit_tokens=True,
        )
        return response.text.strip()

    # 通过统一提示和原生 Pydantic 响应格式调用底层模型。
    async def _request(
        self,
        run_id: str,
        step: int,
        instruction: str,
        payload: dict[str, object],
        model: type[_Model],
    ) -> _Model:
        return await self._provider.complete_structured(
            run_id,
            step,
            [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            response_model=model,
            system_instruction=instruction,
        )


class Planner:
    # 注入满足规划契约的模型适配器以隔离计划生成职责。
    def __init__(self, provider: AutonomyProvider) -> None:
        self._provider = provider # 这里传入的应该是上面创建的 StructuredAutonomyProvider

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
