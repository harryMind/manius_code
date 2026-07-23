import asyncio
from typing import Any

from manius_code.core.config import ManiusConfig, TavilyConfig
from manius_code.core.tools.defaults import default_tool_catalog
from manius_code.core.tools.tavily import TavilyReadArguments, TavilySearchArguments


class FakeTavilyClient:
    # 保存工具传入的参数，以验证 Tavily 适配器不会泄漏 SDK 细节到执行器。
    def __init__(self) -> None:
        self.search_arguments: TavilySearchArguments | None = None
        self.extract_arguments: TavilyReadArguments | None = None

    # 返回包含一个网页摘要的确定性 Tavily Search 响应。
    async def search(self, arguments: TavilySearchArguments) -> dict[str, Any]:
        self.search_arguments = arguments
        return {
            "answer": "Tavily answer",
            "results": [
                {
                    "title": "Tavily Docs",
                    "url": "https://docs.tavily.com",
                    "content": "Search documentation",
                }
            ],
        }

    # 返回包含一个 Markdown 页面正文的确定性 Tavily Extract 响应。
    async def extract(self, arguments: TavilyReadArguments) -> dict[str, Any]:
        self.extract_arguments = arguments
        return {
            "results": [{"url": arguments.urls[0], "raw_content": "# Tavily\nWeb content"}],
            "failed_results": [],
        }


# 功能：验证配置了密钥时会向执行器注入 Tavily 搜索和网页阅读两个独立工具。
# 设计：通过替身客户端构造默认目录并执行两项工具，覆盖组合根、参数映射和输出渲染而不访问 Tavily 服务。
def test_default_tool_catalog_loads_tavily_search_and_read_tools() -> None:
    client = FakeTavilyClient()
    catalog = default_tool_catalog(
        ManiusConfig(tavily=TavilyConfig(api_key="tvly-test")),
        tavily_client_factory=lambda _api_key: client,
    )

    search_result = asyncio.run(catalog.get("web_search").execute({"query": "Tavily API", "max_results": 3}))
    read_result = asyncio.run(catalog.get("web_read").execute({"urls": ["https://docs.tavily.com"]}))

    assert {"read_file", "write_file", "list_dir", "bash", "web_search", "web_read"} == catalog.names()
    assert client.search_arguments is not None
    assert client.search_arguments.query == "Tavily API"
    assert "url: https://docs.tavily.com" in search_result
    assert client.extract_arguments is not None
    assert client.extract_arguments.urls == ["https://docs.tavily.com"]
    assert "# Tavily" in read_result
