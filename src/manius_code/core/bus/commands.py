from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


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


Command = Annotated[
    PingCommand | EventSubscribeCommand | EventUnsubscribeCommand | EventListCommand | AgentRunCommand,
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
