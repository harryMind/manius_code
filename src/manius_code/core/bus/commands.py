from typing import Annotated, Literal

from pydantic import BaseModel, Field


class PingCommand(BaseModel):
    type: Literal["core.ping"] = "core.ping"
    client: str


Command = Annotated[PingCommand, Field(discriminator="type")]


class PongResult(BaseModel):
    server: str
    uptime_ms: int
