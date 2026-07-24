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


# 标识守护进程从已持久化计划恢复一次中断任务。
class RunResumedEvent(BaseEvent):
    type: Literal["run_resumed"] = "run_resumed"
    goal: str
    run_dir: str
    previous_step: int


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


class PlanProposedEvent(BaseEvent):
    type: Literal["plan_proposed"] = "plan_proposed"
    version: int
    plan: dict[str, Any]


class PlanApprovedEvent(BaseEvent):
    type: Literal["plan_approved"] = "plan_approved"
    version: int
    plan_id: str


class PlanRevisedEvent(BaseEvent):
    type: Literal["plan_revised"] = "plan_revised"
    previous_version: int
    version: int
    reason: str


class StepVerifiedEvent(BaseEvent):
    type: Literal["step_verified"] = "step_verified"
    step_id: str
    evidence: list[str]


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


class SessionEventBase(BaseModel):
    kind: Literal["event"] = "event"
    type: str
    session_id: str
    run_id: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    step: int = 0


class SessionCreatedEvent(SessionEventBase):
    type: Literal["session_created"] = "session_created"
    client_id: str | None = None


class SessionMessageSentEvent(SessionEventBase):
    type: Literal["session_message_sent"] = "session_message_sent"
    run_id: str
    content: str


class NoteSavedEvent(SessionEventBase):
    type: Literal["note_saved"] = "note_saved"
    note_id: int
    title: str


AgentEvent = Annotated[
    RunStartedEvent
    | RunResumedEvent
    | RunFinishedEvent
    | StepPlanningEvent
    | StepDoneEvent
    | PlanProposedEvent
    | PlanApprovedEvent
    | PlanRevisedEvent
    | StepVerifiedEvent
    | ToolCallStartEvent
    | ToolCallSuccessEvent
    | ToolCallFailedEvent
    | LlmRequestEvent
    | LlmTokenEvent
    | LlmResponseEvent
    | SessionCreatedEvent
    | SessionMessageSentEvent
    | NoteSavedEvent,
    Field(discriminator="type"),
]


class EventPushEnvelope(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    method: Literal["event.push"] = "event.push"
    params: AgentEvent

Event = Annotated[
    CoreStartedEvent
    | RunStartedEvent
    | RunResumedEvent
    | RunFinishedEvent
    | StepPlanningEvent
    | StepDoneEvent
    | PlanProposedEvent
    | PlanApprovedEvent
    | PlanRevisedEvent
    | StepVerifiedEvent
    | ToolCallStartEvent
    | ToolCallSuccessEvent
    | ToolCallFailedEvent
    | LlmRequestEvent
    | LlmTokenEvent
    | LlmResponseEvent
    | SessionCreatedEvent
    | SessionMessageSentEvent
    | NoteSavedEvent,
    Field(discriminator="type"),
]
