from typing import Annotated, Literal

from pydantic import BaseModel, Field


class CoreStartedEvent(BaseModel):
    type: Literal["core.started"] = "core.started"
    server: str


Event = Annotated[CoreStartedEvent, Field(discriminator="type")]
