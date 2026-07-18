from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class BaseEvent(BaseModel):
    kind: Literal["event"] = "event"
    type: str
    run_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    step: int = 0


class CoreStartedEvent(BaseModel):
    kind: Literal["event"] = "event"
    type: Literal["core.started"] = "core.started"
    server: str


class RunStartedEvent(BaseEvent):
    type: Literal["run_started"] = "run_started"
    goal: str
    run_dir: str


class RunFinishedEvent(BaseEvent):
    type: Literal["run_finished"] = "run_finished"
    status: Literal["success", "failed"]
    total_steps: int
    duration_ms: int
    summary: str = ""
    reason: str | None = None


class StepPlanningEvent(BaseEvent):
    type: Literal["step_planning"] = "step_planning"
    plan: str


class StepDoneEvent(BaseEvent):
    type: Literal["step_done"] = "step_done"
    complete: bool
    observation: str = ""


class ToolCallStartEvent(BaseEvent):
    type: Literal["tool_call_start"] = "tool_call_start"
    tool_name: str
    arguments: dict[str, Any]


class ToolCallSuccessEvent(BaseEvent):
    type: Literal["tool_call_success"] = "tool_call_success"
    tool_name: str
    duration_ms: int
    result: str


class ToolCallFailedEvent(BaseEvent):
    type: Literal["tool_call_failed"] = "tool_call_failed"
    tool_name: str
    duration_ms: int
    error: str


class LlmRequestEvent(BaseEvent):
    type: Literal["llm_request"] = "llm_request"
    messages: list[dict[str, Any]]


class LlmTokenEvent(BaseEvent):
    type: Literal["llm_token"] = "llm_token"
    token: str


class LlmResponseEvent(BaseEvent):
    type: Literal["llm_response"] = "llm_response"
    duration_ms: int
    text: str
    tool_calls: list[dict[str, Any]]


AgentEvent = Annotated[
    RunStartedEvent
    | RunFinishedEvent
    | StepPlanningEvent
    | StepDoneEvent
    | ToolCallStartEvent
    | ToolCallSuccessEvent
    | ToolCallFailedEvent
    | LlmRequestEvent
    | LlmTokenEvent
    | LlmResponseEvent,
    Field(discriminator="type"),
]

Event = Annotated[
    CoreStartedEvent
    | RunStartedEvent
    | RunFinishedEvent
    | StepPlanningEvent
    | StepDoneEvent
    | ToolCallStartEvent
    | ToolCallSuccessEvent
    | ToolCallFailedEvent
    | LlmRequestEvent
    | LlmTokenEvent
    | LlmResponseEvent,
    Field(discriminator="type"),
]
