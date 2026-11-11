"""
见字如面 - 意图识别/改写模块
在知识库检索前，调用 LLM 对用户问题进行意图识别与改写：
  1. 提取关键检索词，改写为更适合向量检索的查询，提高检索准确度；
  2. 同时判定该问题的语义缓存域标签「公共/个人」——供网关层语义缓存决定
     该问答能否跨用户共享复用（公共）还是仅限本用户（个人）。
"""
import json
import logging
import re

from app.core.llm import LLMFactory

logger = logging.getLogger("jianziruyang.intent_rewriter")

REWRITE_SYSTEM_PROMPT = (
    "你是一个查询意图识别助手。你需要对用户问题做两件事，并只输出一个 JSON 对象：\n"
    "1) query：将用户问题改写为更适合向量知识库检索的检索查询——提取核心关键词与检索意图，"
    "去除口语化表述、代词（这个/那个/它等）与冗余修饰，20 字以内为佳；若已是检索式风格则原样返回；"
    "特定概念/术语/名称必须保留。\n"
    "2) scope：判定该问题的答案是否可跨用户共享，取值 public 或 personal——\n"
    "   - public：通用知识/科普/事实/定义类问题，答案对所有用户一致，不依赖任何个人数据。"
    "例如「红枣枸杞的功效」「高血压吃什么好」「今天深圳天气」。\n"
    "   - personal：涉及用户个人数据/记忆/偏好/档案，或第一人称具体处境，答案因人而异。"
    "例如「根据我的体质该怎么调理」「我上次的笔记里提到的方子」「适合我的运动强度」。\n\n"
    "只输出 JSON，形如：{\"query\": \"改写后的检索查询\", \"scope\": \"public\"}，"
    "不要输出任何解释、前后缀或代码块标记。\n\n"
    "示例：\n"
    "用户：我上次看到笔记里说那个什么红枣枸杞的，对睡眠好，能再说说吗？\n"
    "输出：{\"query\": \"红枣枸杞助眠\", \"scope\": \"personal\"}\n\n"
    "用户：最近老是睡不好，有什么好方法吗？\n"
    "输出：{\"query\": \"改善睡眠方法\", \"scope\": \"public\"}\n\n"
    "用户：中医对于脾胃虚弱有什么调理的建议\n"
    "输出：{\"query\": \"中医脾胃虚弱调理\", \"scope\": \"public\"}\n\n"
    "用户：根据我的体质我该怎么补气血\n"
    "输出：{\"query\": \"体质补气血调理\", \"scope\": \"personal\"}"
)

# 从模型输出中提取 JSON 对象（容忍代码块围栏与前后噪声）
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.S)


def _parse_intent_output(raw: str, fallback_query: str) -> dict:
    """稳健解析意图识别输出为 {query, scope}；任何异常回落原句 + personal。"""
    text = (raw or "").strip()
    if text.endswith("/no_think"):
        text = text[: -len("/no_think")].strip()
    scope = "personal"  # 安全默认：解析不确定时不公共共享
    query = ""
    # 剔除可能的 ```json 围栏
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    m = _JSON_OBJ_RE.search(text)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                query = str(obj.get("query") or obj.get("q") or "").strip()
                s = str(obj.get("scope") or "").strip().lower()
                if s in ("public", "personal"):
                    scope = s
        except Exception:
            query = ""
    if not query:
        # 非 JSON 输出：把整段当作改写查询（向后兼容旧纯文本改写模型）
        query = text
    if not query or len(query) > 100:
        query = fallback_query
    return {"query": query, "scope": scope}


async def analyze_intent(query: str, llm_provider: str = None) -> dict:
    """意图识别：一次 LLM 调用同时产出「改写检索查询」与「公共/个人」缓存域标签。

    Args:
        query: 用户原始问题
        llm_provider: 已废弃（保留兼容）；模型统一由网关 intent_rewrite 业务路由解析

    Returns:
        {"query": 改写后的检索查询, "scope": "public"|"personal"}；失败回落原句 + personal。
    """
    if not query or not query.strip():
        return {"query": query, "scope": "personal"}

    try:
        llm = LLMFactory.create_for_role("intent_rewrite")
        messages = [
            {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
            # /no_think：Qwen3 系模型跳过思考链直接输出（其他模型忽略该指令）
            {"role": "user", "content": f"{query}\n/no_think"},
        ]
        collected = ""
        async for chunk in llm.chat(messages, stream=True):
            collected += chunk

        result = _parse_intent_output(collected, query.strip())
        logger.info("意图识别: %r → query=%r scope=%s",
                    query[:50], result["query"][:50], result["scope"])
        return result
    except Exception as e:
        logger.warning("意图识别失败，使用原句 + personal: %s", e)
        return {"query": query.strip(), "scope": "personal"}


async def rewrite_query(query: str, llm_provider: str = None) -> str:
    """使用 LLM 改写用户问题为检索查询（向后兼容：仅返回改写后的字符串）。

    需要「公共/个人」标签时请直接调用 analyze_intent。
    """
    result = await analyze_intent(query, llm_provider)
    return result.get("query") or (query or "").strip()
