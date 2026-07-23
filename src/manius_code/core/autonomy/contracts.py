from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

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


class PlanProposal(BaseModel):
    goal: str
    steps: list[PlanStep] = Field(min_length=1)

    # 拒绝缺少可执行工具或可验证验收条件的模型计划，避免其进入调度器后才失败。
    @model_validator(mode="after")
    def _validate_executable_steps(self) -> "PlanProposal":
        for step in self.steps:
            if not step.allowed_tools:
                raise ValueError(f"step {step.id} must declare allowed tools")
            if not step.acceptance_criteria:
                raise ValueError(f"step {step.id} must declare acceptance criteria")
        return self


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
