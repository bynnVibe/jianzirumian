"""
见字如面 - 联网搜索服务
支持:
  - DuckDuckGo (免费, 无需 API Key)
  - Bing Search API
  - SerpAPI
  - AnySearch (统一实时搜索, Key 可选; Skill 模块见 backend/.skills/anysearch)
"""
import asyncio
import logging
import re
from typing import List, Optional

import httpx

from app.config import settings
from app.services.config_manager import effective_provider_config, runtime_config

logger = logging.getLogger("jianziruyang.search")

# DuckDuckGo 搜索用 User-Agent 列表（轮流使用避免限流）
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/115.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
]

# 共享异步客户端：复用连接池，避免每次搜索重复 TCP+TLS 握手
_shared_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=30, write=30, pool=10),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
    return _shared_client


def _strip_markdown(text: str) -> str:
    """去除摘要中的原始 Markdown 噪声（图片/链接/标题/强调符号），保留纯文本"""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)      # 图片
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # 链接 -> 保留文本
    text = re.sub(r"#{1,6}\s*", "", text)                   # 标题符号
    text = re.sub(r"\*{1,3}|`+", "", text)                  # 加粗/斜体/代码符号
    return re.sub(r"\s+", " ", text).strip()


class SearchResult:
    """搜索结果"""

    def __init__(self, title: str, url: str, snippet: str):
        self.title = title
        self.url = url
        self.snippet = snippet

    def to_dict(self) -> dict:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


class DuckDuckGoSearch:
    """DuckDuckGo 搜索 (免费，带重试)"""

    @staticmethod
    def _search_sync(query: str, max_results: int, ua: str) -> List[SearchResult]:
        """同步执行 DDG 搜索（供线程池调用，避免阻塞事件循环）"""
        from duckduckgo_search import DDGS

        results = []
        with DDGS(headers={"User-Agent": ua}) as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append(
                    SearchResult(
                        title=r.get("title", ""),
                        url=r.get("href", ""),
                        snippet=r.get("body", ""),
                    )
                )
        return results

    async def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        last_error = None
        for attempt in range(3):
            try:
                # 轮流切换 User-Agent 避免限流
                ua = USER_AGENTS[attempt % len(USER_AGENTS)]

                try:
                    results = await asyncio.to_thread(self._search_sync, query, max_results, ua)
                except ImportError:
                    raise ImportError(
                        "duckduckgo-search 未安装。请执行: pip install duckduckgo-search"
                    )
                if results:
                    logger.info("DuckDuckGo 搜索成功: %d 条结果", len(results))
                else:
                    logger.warning("DuckDuckGo 搜索无结果 (尝试 %d/3)", attempt + 1)
                return results

            except ImportError:
                raise
            except Exception as e:
                last_error = e
                logger.warning(
                    "DuckDuckGo 搜索失败 (尝试 %d/3): %s",
                    attempt + 1,
                    e,
                )
                if attempt < 2:
                    # 指数退避: 1s, 2s
                    await asyncio.sleep(1 + attempt)

        logger.error("DuckDuckGo 搜索最终失败: %s", last_error)
        return []


class BingSearch:
    """Bing Search API"""

    def __init__(self):
        self.api_key = effective_provider_config("search", "bing").get("api_key", "")

    async def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        if not self.api_key:
            raise ValueError("未配置 Bing API Key")

        url = "https://api.bing.microsoft.com/v7.0/search"
        headers = {"Ocp-Apim-Subscription-Key": self.api_key}
        params = {"q": query, "count": max_results, "mkt": "zh-CN"}

        resp = await _get_client().get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("webPages", {}).get("value", []):
            results.append(
                SearchResult(
                    title=item.get("name", ""),
                    url=item.get("url", ""),
                    snippet=item.get("snippet", ""),
                )
            )
        return results


class SerpAPISearch:
    """SerpAPI 搜索"""

    def __init__(self):
        self.api_key = effective_provider_config("search", "serpapi").get("api_key", "")

    async def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        if not self.api_key:
            raise ValueError("未配置 SerpAPI Key")

        url = "https://serpapi.com/search"
        params = {
            "q": query,
            "api_key": self.api_key,
            "num": max_results,
            "engine": "google",
            "hl": "zh-cn",
        }

        resp = await _get_client().get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("organic_results", []):
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("link", ""),
                    snippet=item.get("snippet", ""),
                )
            )
        return results


class AnySearchSearch:
    """AnySearch 统一实时搜索

    协议与项目内置 Skill（backend/.skills/anysearch）一致：
    POST https://api.anysearch.com/mcp，JSON-RPC 2.0 tools/call。
    API Key 可选，未配置时匿名访问（限额较低）。
    """

    ENDPOINT = "https://api.anysearch.com/mcp"
    CLIENT_HEADER = "skill/3.0.1"

    def __init__(self):
        self.api_key = effective_provider_config("search", "anysearch").get("api_key", "")

    async def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search",
                "arguments": {"query": query, "max_results": min(max(max_results, 1), 10)},
            },
        }
        headers = {
            "Content-Type": "application/json",
            "X-Anysearch-Client": self.CLIENT_HEADER,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        resp = await _get_client().post(self.ENDPOINT, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            msg = data["error"].get("message", str(data["error"]))
            raise RuntimeError(f"AnySearch API 错误: {msg}")

        # 提取 result.content 中的文本（Markdown 格式搜索结果）
        text = ""
        for item in data.get("result", {}).get("content", []):
            if item.get("type") == "text":
                text = item.get("text", "")
                break

        results = self._parse_markdown(text)
        logger.info("AnySearch 搜索完成: %d 条结果", len(results))
        return results

    @staticmethod
    def _parse_markdown(text: str) -> List[SearchResult]:
        """解析 AnySearch 返回的 Markdown 结果

        格式示例:
            ### 1. 标题
            - **URL**: https://...
            - 摘要文本...
        """
        results = []
        # 按 "### N. " 分块
        blocks = re.split(r"^### \d+\.\s*", text, flags=re.MULTILINE)
        for block in blocks[1:]:
            lines = block.strip().split("\n")
            title = lines[0].strip() if lines else ""
            url = ""
            snippet_parts = []
            for line in lines[1:]:
                line = line.strip()
                m = re.match(r"-\s*\*\*URL\*\*[:：]\s*(\S+)", line)
                if m:
                    url = m.group(1)
                elif line.startswith("- "):
                    snippet_parts.append(line[2:].strip())
                elif line and not line.startswith("#"):
                    snippet_parts.append(line)
            snippet = _strip_markdown(" ".join(snippet_parts))[:500]
            if title:
                results.append(SearchResult(title=_strip_markdown(title), url=url, snippet=snippet))
        return results


class WebSearchFactory:
    """联网搜索工厂"""

    @staticmethod
    def create(provider: str = None):
        provider = provider or runtime_config.search_provider or settings.SEARCH_PROVIDER

        if provider == "duckduckgo":
            return DuckDuckGoSearch()
        elif provider == "bing":
            return BingSearch()
        elif provider == "serpapi":
            return SerpAPISearch()
        elif provider == "anysearch":
            return AnySearchSearch()
        else:
            raise ValueError(f"不支持的搜索提供商: {provider}")


async def web_search(query: str, max_results: int = 5) -> List[SearchResult]:
    """快捷联网搜索"""
    searcher = WebSearchFactory.create()
    return await searcher.search(query, max_results)
