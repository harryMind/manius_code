from collections.abc import Callable

from manius_code.core.config import ManiusConfig
from manius_code.core.tools.bash import BashTool
from manius_code.core.tools.catalog import ToolCatalog
from manius_code.core.tools.file_tools import ListDirTool, WriteFileTool
from manius_code.core.tools.read_file import ReadFileTool
from manius_code.core.tools.tavily import TavilyClient, TavilyReadTool, TavilySdkClient, TavilySearchTool

TavilyClientFactory = Callable[[str], TavilyClient]


# 按配置组装内置工具与可选 Tavily 插件，作为 Agent 依赖注入的唯一组合根。
def default_tool_catalog(config: ManiusConfig, tavily_client_factory: TavilyClientFactory = TavilySdkClient) -> ToolCatalog:
    tools = [ReadFileTool(), WriteFileTool(), ListDirTool(), BashTool()]
    if config.tavily.api_key:
        client = tavily_client_factory(config.tavily.api_key)
        tools.extend([TavilySearchTool(client), TavilyReadTool(client)])
    return ToolCatalog(tools)
