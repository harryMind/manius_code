from __future__ import annotations

from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, Field, ValidationError

from manius_code.core.tools.invocation import ToolExecutionError

_MAX_WEB_CONTENT_CHARS = 60_000


class TavilySearchArguments(BaseModel):
    query: str = Field(min_length=1)
    max_results: int = Field(default=5, ge=1, le=10)
    search_depth: Literal["basic", "advanced"] = "basic"
    include_domains: list[str] = Field(default_factory=list)


class TavilyReadArguments(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=20)
    extract_depth: Literal["basic", "advanced"] = "basic"


class TavilyClient(Protocol):
    # 使用 Tavily Search 查询网页并返回原始响应字典。
    async def search(self, arguments: TavilySearchArguments) -> dict[str, Any]: ...

    # 使用 Tavily Extract 读取网页正文并返回原始响应字典。
    async def extract(self, arguments: TavilyReadArguments) -> dict[str, Any]: ...


class TavilySdkClient:
    # 创建官方异步 SDK 客户端，令网络供应商依赖仅存在于 Tavily 适配层。
    def __init__(self, api_key: str) -> None:
        try:
            from tavily import AsyncTavilyClient
        except ImportError as error:
            raise RuntimeError("Tavily SDK is unavailable; run 'uv sync' to install tavily-python") from error
        self._client = AsyncTavilyClient(api_key=api_key)

    # 将搜索参数映射为官方 SDK 请求并保留其结构化响应。
    async def search(self, arguments: TavilySearchArguments) -> dict[str, Any]:
        response = await self._client.search(
            query=arguments.query,
            max_results=arguments.max_results,
            search_depth=arguments.search_depth,
            include_domains=arguments.include_domains or None,
            include_raw_content=False,
        )
        return dict(response)

    # 将阅读参数映射为官方 SDK Extract 请求并保留 Markdown 内容。
    async def extract(self, arguments: TavilyReadArguments) -> dict[str, Any]:
        response = await self._client.extract(
            urls=arguments.urls,
            extract_depth=arguments.extract_depth,
            include_images=False,
            include_favicon=False,
            format="markdown",
        )
        return dict(response)


class TavilySearchTool:
    name = "web_search"
    arguments_model = TavilySearchArguments

    # 注入 Tavily 客户端，使搜索工具可被替换、测试或卸载而不影响执行器。
    def __init__(self, client: TavilyClient) -> None:
        self._client = client

    # 校验搜索请求、调用 Tavily Search 并输出带链接的精简结果。
    async def execute(self, arguments: dict[str, Any]) -> str:
        try:
            values = TavilySearchArguments.model_validate(arguments)
        except ValidationError as error:
            raise ToolExecutionError(self.name, "requires a non-empty 'query' and valid search options") from error
        try:
            response = await self._client.search(values)
        except Exception as error:
            raise ToolExecutionError(self.name, f"Tavily search failed: {error}") from error
        return _format_search_results(response)


class TavilyReadTool:
    name = "web_read"
    arguments_model = TavilyReadArguments

    # 注入 Tavily 客户端，使网页阅读与具体 HTTP/SDK 实现保持隔离。
    def __init__(self, client: TavilyClient) -> None:
        self._client = client

    # 校验网页地址、调用 Tavily Extract 并输出页面正文与失败原因。
    async def execute(self, arguments: dict[str, Any]) -> str:
        try:
            values = TavilyReadArguments.model_validate(arguments)
        except ValidationError as error:
            raise ToolExecutionError(self.name, "requires one to twenty valid 'urls'") from error
        if any(not _is_http_url(url) for url in values.urls):
            raise ToolExecutionError(self.name, "all urls must use http or https")
        try:
            response = await self._client.extract(values)
        except Exception as error:
            raise ToolExecutionError(self.name, f"Tavily extract failed: {error}") from error
        return _format_extract_results(response)


# 判断输入字符串是否为可交给 Tavily Extract 的绝对 HTTP(S) 地址。
def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


# 将 Tavily Search 的结果规范为带标题、链接和内容摘要的上下文文本。
def _format_search_results(response: dict[str, Any]) -> str:
    answer = response.get("answer")
    lines = [f"answer: {answer}"] if isinstance(answer, str) and answer else []
    results = response.get("results")
    if not isinstance(results, list) or not results:
        return "\n".join(lines) if lines else "no search results"
    for index, result in enumerate(results, start=1):
        if not isinstance(result, dict):
            continue
        title = result.get("title") if isinstance(result.get("title"), str) else "untitled"
        url = result.get("url") if isinstance(result.get("url"), str) else ""
        content = result.get("content") if isinstance(result.get("content"), str) else ""
        lines.extend([f"[{index}] {title}", f"url: {url}", _truncate(content)])
    return "\n".join(lines) if lines else "no search results"


# 将 Tavily Extract 的正文和每个失败地址规范为可供模型继续推理的文本。
def _format_extract_results(response: dict[str, Any]) -> str:
    lines: list[str] = []
    results = response.get("results")
    if isinstance(results, list):
        for index, result in enumerate(results, start=1):
            if not isinstance(result, dict):
                continue
            url = result.get("url") if isinstance(result.get("url"), str) else ""
            content = result.get("raw_content") if isinstance(result.get("raw_content"), str) else ""
            lines.extend([f"[{index}] {url}", _truncate(content)])
    failed_results = response.get("failed_results")
    if isinstance(failed_results, list):
        for failed in failed_results:
            if isinstance(failed, dict):
                url = failed.get("url") if isinstance(failed.get("url"), str) else ""
                error = failed.get("error") if isinstance(failed.get("error"), str) else "unknown error"
                lines.append(f"failed: {url}: {error}")
    return "\n".join(lines) if lines else "no readable web content"


# 对外部网页内容实施上下文上限，避免单个页面耗尽 Agent 的提示词容量。
def _truncate(content: str) -> str:
    if len(content) <= _MAX_WEB_CONTENT_CHARS:
        return content
    return f"{content[:_MAX_WEB_CONTENT_CHARS]}\n[truncated: web content exceeds {_MAX_WEB_CONTENT_CHARS} characters]"
