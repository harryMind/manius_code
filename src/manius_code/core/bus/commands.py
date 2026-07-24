from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator


class PingCommand(BaseModel):
    type: Literal["core.ping"] = "core.ping"
    client: str


class EventSubscribeCommand(BaseModel):
    type: Literal["event.subscribe"] = "event.subscribe"
    run_id: str | None = Field(default=None, pattern="^[A-Za-z0-9_-]+$")
    topics: list[str] = Field(default_factory=lambda: ["*"])


class EventUnsubscribeCommand(BaseModel):
    type: Literal["event.unsubscribe"] = "event.unsubscribe"
    sub_id: str


class EventListCommand(BaseModel):
    type: Literal["event.list"] = "event.list"
    run_id: str = Field(pattern="^[A-Za-z0-9_-]+$")


class AgentRunCommand(BaseModel):
    type: Literal["agent.run"] = "agent.run"
    goal: str = Field(min_length=1)


# 恢复指定运行目录中已持久化且仍可继续调度的计划。
class AgentResumeCommand(BaseModel):
    type: Literal["agent.resume"] = "agent.resume"
    run_id: str = Field(pattern="^[A-Za-z0-9_-]+$")


class SessionCreateCommand(BaseModel):
    type: Literal["session.create"] = "session.create"
    client_id: str | None = Field(default=None, max_length=256)


class SessionSendCommand(BaseModel):
    type: Literal["session.send"] = "session.send"
    session_id: str = Field(pattern="^[A-Za-z0-9_-]+$")
    content: str = Field(min_length=1)

    # 拒绝仅由空白组成的会话输入，避免创建无法执行的后台运行。
    @field_validator("content")
    @classmethod
    def _content_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


class SessionGetCommand(BaseModel):
    type: Literal["session.get"] = "session.get"
    session_id: str = Field(pattern="^[A-Za-z0-9_-]+$")


class SessionListCommand(BaseModel):
    type: Literal["session.list"] = "session.list"


class SessionDestroyCommand(BaseModel):
    type: Literal["session.destroy"] = "session.destroy"
    session_id: str = Field(pattern="^[A-Za-z0-9_-]+$")


Command = Annotated[
    PingCommand
    | EventSubscribeCommand
    | EventUnsubscribeCommand
    | EventListCommand
    | AgentRunCommand
    | AgentResumeCommand
    | SessionCreateCommand
    | SessionSendCommand
    | SessionGetCommand
    | SessionListCommand
    | SessionDestroyCommand,
    Field(discriminator="type"),
]


class PongResult(BaseModel):
    server: str
    uptime_ms: int


class EventSubscribeResult(BaseModel):
    subscribed: bool = True
    sub_id: str
    run_id: str | None = None
    topics: list[str]


class EventUnsubscribeResult(BaseModel):
    unsubscribed: bool


class EventListResult(BaseModel):
    run_id: str
    events: list[dict[str, Any]]


class AgentRunResult(BaseModel):
    run_id: str


class SessionMetaResult(BaseModel):
    session_id: str
    client_id: str | None = None
    created_at: datetime
    updated_at: datetime
    run_ids: list[str] = Field(default_factory=list)
    turn_count: int = Field(default=0, ge=0)


class SessionThreadEntryResult(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    run_id: str | None = None
    timestamp: datetime


class SessionCreateResult(BaseModel):
    session: SessionMetaResult


class SessionSendResult(BaseModel):
    session_id: str
    run_id: str


class SessionGetResult(BaseModel):
    session: SessionMetaResult
    thread: list[SessionThreadEntryResult] = Field(default_factory=list)


class SessionListResult(BaseModel):
    sessions: list[SessionMetaResult] = Field(default_factory=list)


class SessionDestroyResult(BaseModel):
    session_id: str
    destroyed: bool
