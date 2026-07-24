from enum import Enum
from typing import Mapping, Union

from pydantic import BaseModel, Field, create_model

from manius_code.core.autonomy.contracts import PlanStep

# 依据步骤标识和工具白名单创建 API 可约束的 Pydantic 动作响应模型。
def action_response_model(
    step_id: str,
    allowed_tools: list[str],
    tool_argument_models: Mapping[str, type[BaseModel]],
) -> type[BaseModel]:
    tool_names = tuple(sorted(set(allowed_tools)))
    if not tool_names or any(tool_name not in tool_argument_models for tool_name in tool_names):
        unavailable_tools = sorted(set(tool_names) - set(tool_argument_models))
        raise ValueError(f"cannot create structured action schema for tools: {unavailable_tools}")
    variants = tuple(
        _action_variant(step_id, tool_name, tool_argument_models[tool_name])
        for tool_name in tool_names
    )
    action_type = Union.__getitem__(variants)
    return create_model(
        "StructuredActionEnvelope_" + "_".join(tool_names),
        action=(action_type, ...),
    )


# 依据当前滚动批次的步骤白名单创建一次返回全部动作的原生结构化响应模型。
def batch_action_response_model(
    steps: list[PlanStep],
    tool_argument_models: Mapping[str, type[BaseModel]],
) -> type[BaseModel]:
    if not steps:
        raise ValueError("cannot create a structured batch action schema without steps")
    variants = tuple(
        _action_variant(step.id, step.allowed_tools[0], tool_argument_models[step.allowed_tools[0]], index)
        for index, step in enumerate(steps)
    )
    action_type = Union.__getitem__(variants)

    class StructuredBatchActionEnvelope(BaseModel):
        actions: list[action_type] = Field(min_length=len(steps), max_length=len(steps))

    return StructuredBatchActionEnvelope


# 为单个允许工具创建固定参数模型和枚举工具名的动作分支。
def _action_variant(
    step_id: str,
    tool_name: str,
    arguments_model: type[BaseModel],
    index: int | None = None,
) -> type[BaseModel]:
    step_id_enum = Enum("StructuredStep_" + step_id, {"current": step_id})
    tool_name_enum = Enum("StructuredTool_" + tool_name, {tool_name: tool_name})
    suffix = "" if index is None else f"_{index}"
    return create_model(
        "StructuredAction_" + tool_name + suffix,
        step_id=(step_id_enum, ...),
        tool_name=(tool_name_enum, ...),
        arguments=(arguments_model, ...),
        rationale=(str, ""),
    )
