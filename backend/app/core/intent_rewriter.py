"""
见字如面 - 意图改写模块
在知识库检索前，调用 LLM 对用户问题进行意图改写，
提取关键检索词，提高向量检索的准确度。
"""
import json
import logging

from app.core.llm import LLMFactory

logger = logging.getLogger("jianziruyang.intent_rewriter")

REWRITE_SYSTEM_PROMPT = (
    "你是一个查询改写助手。你的任务是将用户的自然语言问题改写为更适合"
    "向量知识库检索的检索查询。\n\n"
    "要求：\n"
    "1. 提取问题的核心关键词和检索意图\n"
    "2. 去除口语化表述、代词（这个、那个、它等）、冗余修饰语\n"
    "3. 改写为简洁、明确的检索语句（20 字以内为佳）\n"
    "4. 如果问题中提到了特定概念、术语、名称，必须保留\n"
    "5. 如果问题很短且已经是检索式风格，直接返回原句\n"
    "6. 只输出改写后的检索查询，不要添加任何解释、前缀或标点\n\n"
    "示例：\n"
    "用户：我上次看到笔记里说那个什么红枣枸杞的，对睡眠好，能再说说吗？\n"
    "改写：红枣枸杞助眠\n\n"
    "用户：最近老是睡不好，有什么好方法吗？\n"
    "改写：改善睡眠方法\n\n"
    "用户：中医对于脾胃虚弱有什么调理的建议\n"
    "改写：中医脾胃虚弱调理\n\n"
    "用户：当归\n"
    "改写：当归"
)


async def rewrite_query(query: str, llm_provider: str = None) -> str:
    """使用 LLM 改写用户问题为检索查询

    Args:
        query: 用户原始问题
        llm_provider: LLM 提供商（可选）

    Returns:
        改写后的检索查询
    """
    if not query or not query.strip():
        return query

    try:
        llm = LLMFactory.create(llm_provider)
        messages = [
            {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ]

        collected = ""
        async for chunk in llm.chat(messages, stream=True):
            collected += chunk

        rewritten = collected.strip()
        # 保底：如果改写为空或过长，返回原句
        if not rewritten or len(rewritten) > 100:
            logger.warning("意图改写结果异常，使用原句: rewritten=%r", rewritten)
            return query.strip()

        logger.info("意图改写: %r → %r", query[:50], rewritten[:50])
        return rewritten

    except Exception as e:
        logger.warning("意图改写失败，使用原句: %s", e)
        return query.strip()
