from enum import Enum
from typing import Union

from pydantic import BaseModel, create_model

from manius_code.core.tools.bash import BashArguments
from manius_code.core.tools.file_tools import ListDirectoryArguments, WriteFileArguments
from manius_code.core.tools.read_file import ReadFileArguments

_TOOL_ARGUMENT_MODELS: dict[str, type[BaseModel]] = {
    "bash": BashArguments,
    "list_dir": ListDirectoryArguments,
    "read_file": ReadFileArguments,
    "write_file": WriteFileArguments,
}


# 依据步骤标识和工具白名单创建 API 可约束的 Pydantic 动作响应模型。
def action_response_model(step_id: str, allowed_tools: list[str]) -> type[BaseModel]:
    tool_names = tuple(sorted(set(allowed_tools)))
    if not tool_names or any(tool_name not in _TOOL_ARGUMENT_MODELS for tool_name in tool_names):
        unavailable_tools = sorted(set(tool_names) - set(_TOOL_ARGUMENT_MODELS))
        raise ValueError(f"cannot create structured action schema for tools: {unavailable_tools}")
    variants = tuple(
        _action_variant(step_id, tool_name)
        for tool_name in tool_names
    )
    action_type = Union.__getitem__(variants)
    return create_model(
        "StructuredActionEnvelope_" + "_".join(tool_names),
        action=(action_type, ...),
    )


# 为单个允许工具创建固定参数模型和枚举工具名的动作分支。
def _action_variant(step_id: str, tool_name: str) -> type[BaseModel]:
    step_id_enum = Enum("StructuredStep_" + step_id, {"current": step_id})
    tool_name_enum = Enum("StructuredTool_" + tool_name, {tool_name: tool_name})
    return create_model(
        "StructuredAction_" + tool_name,
        step_id=(step_id_enum, ...),
        tool_name=(tool_name_enum, ...),
        arguments=(_TOOL_ARGUMENT_MODELS[tool_name], ...),
        rationale=(str, ""),
    )
