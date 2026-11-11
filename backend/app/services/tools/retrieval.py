"""
见字如面 - 检索类工具（联网搜索 / 知识库检索）

把原本内联在 chat_agent / chat.py 中的两个检索能力封装为注册表一等公民工具，
补齐命名空间、标签、适用场景、参数 Schema、权限、风险等级与可用状态，
使其可被 Tool Routing（场景感知路由）、调用前约束钩子、function calling
schema 导出与 Tool Routing 可观测性统一治理。

  - web_search：联网搜索公开网络资料，适用场景显式覆盖「实时/时效性查询」
    （天气、新闻、股价、赛事、今日/最近发生的事），是天气类问题答非所问的修复关键——
    路由层据此把实时查询的主来源指向联网搜索而非健康养生知识库。
  - search_knowledge：在用户可访问的健康养生知识库中检索原文依据。

两个工具均为只读（risk_level=low），返回 ToolResult；原始命中对象通过
extra["_raw"] 透传给调用方（chat_agent 累积状态用），不参与可观测性序列化。
"""
import asyncio
import logging
from typing import List

from app.services.tools.base import BaseTool, ToolContext, ToolResult, register_tool

logger = logging.getLogger("jianziruyang.tools.retrieval")


class WebSearchTool(BaseTool):
    name = "web_search"
    description = (
        "联网搜索公开网络资料。适用于实时/时效性问题（天气、新闻、股价、赛事、"
        "今日或最近发生的事），以及知识库没有相关内容时的公开资料补充。"
        "搜索结果不属于用户知识库，回答时须注明来源链接。"
    )
    namespace = "retrieval"
    tags = ("search", "web", "realtime", "readonly", "external")
    scenarios = (
        "查询实时/时效性信息：天气、气温、下雨、新闻、股价、汇率、赛事比分",
        "问题含「今天/今日/现在/最新/最近/实时」等时效词",
        "知识库没有相关内容时补充公开网络资料",
        "需要最新公开数据而非知识库静态内容",
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "联网搜索关键词（精炼、去除口语与代词）"},
            "max_results": {"type": "integer", "description": "最大返回结果数（默认 5）", "default": 5},
        },
        "required": ["query"],
    }
    permissions = ()
    risk_level = "low"
    status = "active"

    async def run(self, ctx: ToolContext, query: str = "", max_results: int = 5, **kwargs) -> ToolResult:
        from app.services.web_search import web_search

        query = (query or "").strip()
        if not query:
            return ToolResult(
                tool_name=self.name, success=False,
                summary="联网搜索关键词为空，请提供 query。", error="empty_query",
            )
        try:
            results = await web_search(query, max_results)
        except Exception as e:
            logger.warning("联网搜索执行失败: %s", e)
            return ToolResult(
                tool_name=self.name, success=False,
                summary=f"联网搜索出错：{e}", error=str(e),
            )
        if not results:
            return ToolResult(
                tool_name=self.name, success=True, text="",
                summary="联网搜索未返回结果（可能是网络受限或搜索服务未配置）。",
                extra={"results": [], "_raw": []},
            )
        parts = [f"[网络{i}] {r.title}\n{r.url}\n{r.snippet}" for i, r in enumerate(results, start=1)]
        return ToolResult(
            tool_name=self.name, success=True, text="\n\n".join(parts),
            summary=f"联网搜索到 {len(results)} 条结果",
            extra={"results": [r.to_dict() for r in results], "_raw": results},
        )


class KnowledgeSearchTool(BaseTool):
    name = "search_knowledge"
    description = (
        "在用户可访问的健康养生知识库中检索与 query 相关的资料片段。"
        "涉及健康/养生/中医等知识的问题应优先调用。"
    )
    namespace = "retrieval"
    tags = ("search", "knowledge", "rag", "readonly", "health")
    scenarios = (
        "回答健康/养生/中医等知识库覆盖的问题",
        "需要从用户私有/公共知识库检索原文依据并标注来源",
        "涉及穴位、食材、功效、调理等静态知识",
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "知识库检索关键词（精炼、去除口语与代词）"},
        },
        "required": ["query"],
    }
    permissions = ()
    risk_level = "low"
    status = "active"

    async def run(self, ctx: ToolContext, query: str = "", **kwargs) -> ToolResult:
        from app.services.knowledge import knowledge_service

        query = (query or "").strip()
        if not query:
            return ToolResult(
                tool_name=self.name, success=False,
                summary="检索关键词为空，请提供 query。", error="empty_query",
            )
        try:
            results = await asyncio.to_thread(
                knowledge_service.retrieve_for_user, query, ctx.user_id
            )
        except Exception as e:
            logger.warning("知识库检索执行失败: %s", e)
            return ToolResult(
                tool_name=self.name, success=False,
                summary=f"知识库检索出错：{e}", error=str(e),
            )
        if not results:
            return ToolResult(
                tool_name=self.name, success=True, text="",
                summary="知识库中未检索到与该问题相关的资料。",
                extra={"results": [], "_raw": []},
            )
        return ToolResult(
            tool_name=self.name, success=True,
            text=f"检索到 {len(results)} 条相关片段",
            summary=f"检索到 {len(results)} 条相关片段",
            extra={"count": len(results), "_raw": results},
        )


web_search_tool = register_tool(WebSearchTool())
knowledge_search_tool = register_tool(KnowledgeSearchTool())
