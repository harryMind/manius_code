from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

StepStatus = Literal["pending", "ready", "running", "verifying", "succeeded", "retryable", "replan_required", "failed"]
ResolverAction = Literal["retry", "revise_step", "replan", "abort"]
CriterionKind = Literal["file_exists", "file_contains", "tool_result_contains"]


class AcceptanceCriterion(BaseModel):
    kind: CriterionKind
    path: str | None = None
    expected: str | None = None


class PlanStep(BaseModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = ""
    dependencies: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    status: StepStatus = "pending"
    attempt_count: int = 0
    last_error: str | None = None


class PlannedStep(PlanStep):
    allowed_tools: list[str] = Field(min_length=1)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)


class PlanProposal(BaseModel):
    goal: str
    steps: list[PlannedStep] = Field(min_length=1)

    # 将内部或测试构造的普通计划步骤转换为严格的模型响应步骤，以保持持久化模型兼容。
    @field_validator("steps", mode="before")
    @classmethod
    def _coerce_steps(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return [item.model_dump(mode="json") if isinstance(item, PlanStep) else item for item in value]


class Plan(BaseModel):
    plan_id: str = Field(default_factory=lambda: uuid4().hex)
    version: int = Field(ge=1)
    goal: str
    steps: list[PlanStep]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ActionProposal(BaseModel):
    step_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""


class StepResult(BaseModel):
    step_id: str
    attempt: int
    tool_name: str | None = None
    observation: str = ""
    error: str | None = None


class VerificationResult(BaseModel):
    passed: bool
    evidence: list[str] = Field(default_factory=list)
    reason: str | None = None


class ResolverDecision(BaseModel):
    action: ResolverAction
    reason: str
    plan: PlanProposal | None = None
    revised_step: PlanStep | None = None
