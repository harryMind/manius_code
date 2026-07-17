from typing import Annotated, Literal

from pydantic import BaseModel, Field


class PingCommand(BaseModel):
    type: Literal["core.ping"] = "core.ping"
    client: str


class EventSubscribeCommand(BaseModel):
    type: Literal["event.subscribe"] = "event.subscribe"


class AgentRunCommand(BaseModel):
    type: Literal["agent.run"] = "agent.run"
    goal: str = Field(min_length=1)


Command = Annotated[PingCommand | EventSubscribeCommand | AgentRunCommand, Field(discriminator="type")]


class PongResult(BaseModel):
    server: str
    uptime_ms: int


class EventSubscribeResult(BaseModel):
    subscribed: bool = True


class AgentRunResult(BaseModel):
    run_id: str
