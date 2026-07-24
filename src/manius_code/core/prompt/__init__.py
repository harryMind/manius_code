"""集中管理可由核心运行时按需加载的 LLM 提示词。"""

from manius_code.core.prompt.agent import legacy_agent_instruction
from manius_code.core.prompt.autonomy import (
    action_instruction,
    batch_action_instruction,
    plan_instruction,
    resolver_instruction,
    summary_instruction,
)

__all__ = [
    "action_instruction",
    "batch_action_instruction",
    "legacy_agent_instruction",
    "plan_instruction",
    "resolver_instruction",
    "summary_instruction",
]
