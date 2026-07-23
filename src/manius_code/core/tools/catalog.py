from collections.abc import Awaitable, Iterable
from typing import Any, Protocol

from pydantic import BaseModel


class ExecutableTool(Protocol):
    name: str
    arguments_model: type[BaseModel]

    # 执行已校验的工具参数并返回可写入 Agent 上下文的文本观察结果。
    def execute(self, arguments: dict[str, Any]) -> Awaitable[str]: ...


class ToolCatalog:
    # 接收一组独立工具并建立名称到实现及参数模型的只读索引。
    def __init__(self, tools: Iterable[ExecutableTool]) -> None:
        self._tools: dict[str, ExecutableTool] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name: {tool.name}")
            self._tools[tool.name] = tool

    # 返回允许规划器声明和执行器调用的工具名称集合。
    def names(self) -> set[str]:
        return set(self._tools)

    # 按名称获取一个已注入的工具实现并在缺失时明确失败。
    def get(self, name: str) -> ExecutableTool:
        try:
            return self._tools[name]
        except KeyError as error:
            raise KeyError(f"tool is not available: {name}") from error

    # 返回原生结构化输出生成动作 Schema 所需的参数模型映射。
    def argument_models(self) -> dict[str, type[BaseModel]]:
        return {name: tool.arguments_model for name, tool in self._tools.items()}
