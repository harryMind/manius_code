from collections.abc import Awaitable
from typing import Any, Protocol


class Tool(Protocol):
    name: str
    definition: dict[str, Any]

    # 执行具体工具逻辑并返回可写入上下文的文本结果。
    def execute(self, arguments: dict[str, Any]) -> Awaitable[str]: ...


class ToolRegistry:
    # 初始化空的工具注册表。
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # 使用工具名称注册一个仅负责具体执行的工具。
    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    # 根据名称查看已注册工具，不存在时抛出查询错误。
    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as error:
            raise KeyError(f"Tool is not registered: {name}") from error

    # 返回可传递给 Anthropic API 的工具定义列表。
    def definitions(self) -> list[dict[str, Any]]:
        return [tool.definition for tool in self._tools.values()]
