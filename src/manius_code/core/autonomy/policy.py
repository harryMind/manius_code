from pydantic import BaseModel, Field


class AutonomyPolicy(BaseModel):
    max_steps: int = Field(default=20, ge=1)
    max_attempts_per_step: int = Field(default=2, ge=1)
    max_plan_versions: int = Field(default=3, ge=1)
