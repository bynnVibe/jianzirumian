"""
见字如面 - llm-wiki 对话/检索历史知识沉淀（路径A：高频主题 → digest 私人综合词条）

沿用 llm-wiki「LLM 作为知识编译器」的方法论，把沉淀落点放在已有的 digest
（wiki.synthesize 跨素材综合词条）上，仅新增一条触发源：

    用户对话/检索历史（chat_messages ⋈ chat_sessions.user_id）
        → LLM 归纳「反复被问及」的高频主题（质量闸门：命中 ≥N 次）
        → 对每个未沉淀过的主题调用 synthesize(topic, user, visibility=private)
        → 生成 visibility=private, owner_id=用户 的私人综合词条（回写向量库参与检索）
        → 落 wiki_topic_digest_log 防重复编译（UNIQUE(user_id, topic_hash)）

由此形成「问得越多 → 私人百科越丰富 → 检索越准」的正向飞轮。

设计原则（对齐既有编译内核）：
- 独立开关 WIKI_CONVERSATION_COMPILE_ENABLED，且受总开关 WIKI_COMPILE_ENABLED 约束；
- 质量闸门：仅沉淀跨多条提问反复命中（hits ≥ WIKI_CONVERSATION_MIN_HITS）的主题；
- 失败静默降级：主题归纳/综合任何异常都只记日志，绝不阻塞问答主链路；
- 周期任务只由持有写者锁的 Agent 进程执行（runtime.owns_process），避免多进程重复编译。
"""
import asyncio
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from app.config import settings
from app.core import db

logger = logging.getLogger("jianziruyang.wiki.conversation")

# 主题归纳提示词：从近期提问列表中蒸馏「反复出现、值得沉淀」的高频主题
_DISTILL_PROMPT = (
    "以下是某用户近期在健康养生知识库中的提问列表。请归纳出其中【反复出现、值得沉淀为"
    "百科词条】的高频主题。规则：\n"
    "- 只统计跨多条提问反复出现的主题（同一主题被问及 ≥{min_hits} 次才算高频）\n"
    "- 主题用简洁的中文名词短语表述（如「高血压的饮食调理」「艾灸的作用与禁忌」），"
    "便于跨素材综合检索\n"
    "- 忽略一次性提问、寒暄、与知识库无关的闲聊\n"
    "- 最多输出 {max_topics} 个，按命中次数从高到低\n"
    '仅输出 JSON 数组，每项 {{"topic": str, "hits": int}}；若无明显高频主题输出 []。'
)

# 周期任务引用（防止 pending 期被 GC）
_PERIODIC_TASK: Optional[asyncio.Task] = None


def conversation_compile_enabled() -> bool:
    """对话沉淀独立开关（同时受知识编译总开关约束）。"""
    from app.services.wiki import wiki_compile_enabled

    return bool(settings.WIKI_CONVERSATION_COMPILE_ENABLED) and wiki_compile_enabled()


def topic_hash(user_id: str, topic: str) -> str:
    """(user_id, 归一化主题) → 稳定哈希，用于防重复沉淀。"""
    norm = re.sub(r"\s+", "", (topic or "").strip().lower())
    return hashlib.sha256(f"{user_id}::{norm}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 对话历史读取
# ---------------------------------------------------------------------------

def _cutoff(window_days: Optional[int] = None) -> str:
    days = window_days if window_days is not None else settings.WIKI_CONVERSATION_WINDOW_DAYS
    return (datetime.now() - timedelta(days=max(1, int(days)))).isoformat()


def recent_user_queries(user_id: str, *, window_days: Optional[int] = None,
                        limit: Optional[int] = None,
                        min_len: Optional[int] = None) -> List[str]:
    """取用户近期提问（chat_messages ⋈ chat_sessions.user_id，role='user'）。

    chat_messages 无 user_id，须经 session_id 关联 chat_sessions 才能按用户归属。
    过滤过短寒暄（< min_len），按时间倒序取 limit 条。
    """
    limit = limit if limit is not None else settings.WIKI_CONVERSATION_MAX_QUERIES
    min_len = min_len if min_len is not None else settings.WIKI_CONVERSATION_MIN_QUERY_LEN
    rows = db.query(
        "SELECT m.content AS content FROM chat_messages m"
        " JOIN chat_sessions s ON m.session_id = s.session_id"
        " WHERE s.user_id = ? AND m.role = 'user' AND m.created_at >= ?"
        " ORDER BY m.created_at DESC LIMIT ?",
        (user_id, _cutoff(window_days), int(limit)),
    )
    queries: List[str] = []
    for r in rows:
        content = (r.get("content") or "").strip()
        if len(content) >= min_len:
            queries.append(content)
    return queries


def list_active_users(*, window_days: Optional[int] = None,
                      min_queries: Optional[int] = None) -> List[str]:
    """近期提问数达质量闸门的用户（排除空/游客），按提问量倒序。"""
    min_queries = min_queries if min_queries is not None else settings.WIKI_CONVERSATION_MIN_HITS
    rows = db.query(
        "SELECT s.user_id AS uid, COUNT(*) AS n FROM chat_messages m"
        " JOIN chat_sessions s ON m.session_id = s.session_id"
        " WHERE m.role = 'user' AND m.created_at >= ?"
        " AND s.user_id <> '' AND s.user_id <> 'guest'"
        " GROUP BY s.user_id HAVING n >= ? ORDER BY n DESC",
        (_cutoff(window_days), int(min_queries)),
    )
    return [r["uid"] for r in rows if r.get("uid")]


# ---------------------------------------------------------------------------
# 高频主题归纳（LLM）+ 解析（纯函数，可离线测试）
# ---------------------------------------------------------------------------

def parse_topics(raw: str, *, min_hits: int = 1, max_topics: int = 10) -> List[dict]:
    """从 LLM 输出解析高频主题数组（容忍代码块/前后缀噪声）。

    过滤 hits < min_hits 的噪声主题、去重、按 hits 倒序取前 max_topics 个。
    """
    text = (raw or "").strip()
    if not text:
        return []
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    out: List[dict] = []
    seen = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        topic = (it.get("topic") or "").strip()
        if not topic:
            continue
        try:
            hits = int(it.get("hits", 0))
        except (TypeError, ValueError):
            hits = 0
        if hits < min_hits:
            continue
        key = re.sub(r"\s+", "", topic.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"topic": topic[:60], "hits": hits})
    out.sort(key=lambda x: x["hits"], reverse=True)
    return out[:max_topics]


async def distill_topics(queries: List[str], *, min_hits: int, max_topics: int) -> List[dict]:
    """调用 LLM 从提问列表归纳高频主题；任何异常静默返回空（不阻塞主链路）。"""
    if not queries:
        return []
    try:
        from app.core.llm import LLMFactory

        llm = LLMFactory.create_for_role("wiki_compile")
    except Exception as e:
        logger.warning("[WIKI-CONV] 主题归纳模型初始化失败（忽略）: %s", e)
        return []
    numbered = "\n".join(
        f"{i + 1}. {q[:120]}" for i, q in enumerate(queries[: settings.WIKI_CONVERSATION_MAX_QUERIES])
    )
    prompt = (
        _DISTILL_PROMPT.format(min_hits=min_hits, max_topics=max_topics)
        + "\n\n提问列表：\n" + numbered
    )
    try:
        collected = ""
        async for chunk in llm.chat([{"role": "user", "content": prompt}], stream=False):
            collected += chunk
        return parse_topics(collected, min_hits=min_hits, max_topics=max_topics)
    except Exception as e:
        logger.warning("[WIKI-CONV] 主题归纳失败（忽略）: %s", e)
        return []


# ---------------------------------------------------------------------------
# 沉淀日志（防重复编译）
# ---------------------------------------------------------------------------

def already_digested(user_id: str, topic: str) -> bool:
    row = db.query_one(
        "SELECT 1 AS ok FROM wiki_topic_digest_log WHERE user_id = ? AND topic_hash = ?",
        (user_id, topic_hash(user_id, topic)),
    )
    return bool(row)


def log_digest(user_id: str, topic: str, page_id: str, hits: int) -> None:
    """登记一条已沉淀主题（幂等：同 (user_id, topic_hash) 覆盖更新）。"""
    now = datetime.now().isoformat()
    db.execute(
        "INSERT INTO wiki_topic_digest_log"
        " (id, user_id, topic, topic_hash, page_id, hits, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(user_id, topic_hash) DO UPDATE SET"
        " page_id = excluded.page_id, hits = excluded.hits, updated_at = excluded.updated_at",
        (str(uuid.uuid4()), user_id, (topic or "")[:120], topic_hash(user_id, topic),
         page_id or "", int(hits or 0), now, now),
    )


def forget_topic(page_id: str) -> None:
    """词条被删除时联动清理沉淀日志，允许该主题日后重新沉淀。"""
    if page_id:
        db.execute("DELETE FROM wiki_topic_digest_log WHERE page_id = ?", (page_id,))


def digest_log(user_id: str, *, limit: int = 50) -> List[dict]:
    return db.query(
        "SELECT topic, page_id, hits, created_at, updated_at FROM wiki_topic_digest_log"
        " WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
        (user_id, int(limit)),
    )


# ---------------------------------------------------------------------------
# 沉淀编排
# ---------------------------------------------------------------------------

async def compile_for_user(user_id: str, *, force: bool = False,
                           max_topics: Optional[int] = None,
                           min_hits: Optional[int] = None,
                           window_days: Optional[int] = None) -> dict:
    """为单个用户沉淀高频主题 → 私人 digest 词条。失败静默，返回生成明细。"""
    if not user_id or user_id == "guest":
        return {"user_id": user_id, "generated": 0, "skipped": "invalid_user"}
    if not conversation_compile_enabled():
        return {"user_id": user_id, "generated": 0, "skipped": "disabled"}

    from app.services.wiki import wiki_service

    max_topics = max_topics if max_topics is not None else settings.WIKI_CONVERSATION_MAX_TOPICS
    min_hits = min_hits if min_hits is not None else settings.WIKI_CONVERSATION_MIN_HITS

    queries = recent_user_queries(user_id, window_days=window_days)
    if len(queries) < min_hits:
        return {"user_id": user_id, "generated": 0, "skipped": "insufficient_queries",
                "queries": len(queries)}

    # 过量归纳，给「已沉淀跳过」留出余量，尽量凑满 max_topics 条新词条
    candidates = await distill_topics(queries, min_hits=min_hits, max_topics=max_topics * 3)
    generated = 0
    topics: List[dict] = []
    for cand in candidates:
        if generated >= max_topics:
            break
        topic, hits = cand["topic"], cand["hits"]
        if not force and already_digested(user_id, topic):
            continue
        try:
            page = await wiki_service.synthesize(
                topic, user_id, visibility="private", owner_id=user_id
            )
        except Exception as e:
            # 无相关素材 / LLM 失败：静默跳过（不登记，日后素材补齐可重试）
            logger.info("[WIKI-CONV] 主题《%s》综合失败（跳过）: %s", topic, e)
            continue
        log_digest(user_id, topic, page.get("id", ""), hits)
        generated += 1
        topics.append({"topic": topic, "hits": hits,
                       "page_id": page.get("id", ""), "title": page.get("title", topic)})
    if generated:
        logger.info("[WIKI-CONV] 用户 %s 对话沉淀：%d 条私人词条", user_id, generated)
    return {"user_id": user_id, "generated": generated, "topics": topics}


async def run_conversation_digest(user_id: Optional[str] = None) -> dict:
    """全量（或指定用户）对话沉淀入口：周期任务与手动触发共用。"""
    if not conversation_compile_enabled():
        return {"generated": 0, "users": 0, "skipped": "disabled"}
    users = [user_id] if user_id else list_active_users()
    total = 0
    details: List[dict] = []
    for u in users:
        try:
            res = await compile_for_user(u)
        except Exception as e:
            logger.warning("[WIKI-CONV] 用户 %s 沉淀异常（跳过）: %s", u, e)
            continue
        total += int(res.get("generated", 0))
        details.append(res)
    return {"generated": total, "users": len(users), "details": details}


async def topics_preview(user_id: str, *, refresh: bool = True) -> dict:
    """前端预览：已沉淀词条日志 + （可选）当前归纳出的高频候选主题。"""
    digested = digest_log(user_id)
    candidates: List[dict] = []
    if refresh and conversation_compile_enabled():
        queries = recent_user_queries(user_id)
        if len(queries) >= settings.WIKI_CONVERSATION_MIN_HITS:
            raw = await distill_topics(
                queries,
                min_hits=settings.WIKI_CONVERSATION_MIN_HITS,
                max_topics=settings.WIKI_CONVERSATION_MAX_TOPICS * 2,
            )
            for t in raw:
                candidates.append({"topic": t["topic"], "hits": t["hits"],
                                   "digested": already_digested(user_id, t["topic"])})
    return {"enabled": conversation_compile_enabled(),
            "min_hits": settings.WIKI_CONVERSATION_MIN_HITS,
            "digested": digested, "candidates": candidates}


# ---------------------------------------------------------------------------
# 周期任务（仅持有写者锁的 Agent 进程执行）
# ---------------------------------------------------------------------------

async def _periodic_loop() -> None:
    interval = max(60.0, float(settings.WIKI_CONVERSATION_INTERVAL_HOURS) * 3600.0)
    # 首次延迟：避开启动高峰（向量库恢复/存量补编译），最长不超过 5 分钟
    await asyncio.sleep(min(300.0, interval))
    while True:
        try:
            from app.core.agent_runtime import runtime
            if runtime.owns_process() and conversation_compile_enabled():
                res = await run_conversation_digest()
                if res.get("generated"):
                    logger.info("[WIKI-CONV] 周期沉淀完成：新增 %d 条私人词条", res["generated"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[WIKI-CONV] 周期沉淀任务失败（忽略）: %s", e)
        await asyncio.sleep(interval)


def start_periodic() -> None:
    """在事件循环内启动周期沉淀任务（需在 lifespan 中调用）。"""
    global _PERIODIC_TASK
    if not settings.WIKI_CONVERSATION_COMPILE_ENABLED:
        return
    if _PERIODIC_TASK and not _PERIODIC_TASK.done():
        return
    try:
        _PERIODIC_TASK = asyncio.create_task(_periodic_loop())
        logger.info("[WIKI-CONV] 对话沉淀周期任务已启动（间隔 %.1fh）",
                    settings.WIKI_CONVERSATION_INTERVAL_HOURS)
    except RuntimeError:
        logger.warning("[WIKI-CONV] 无运行中的事件循环，周期沉淀任务未启动")


async def stop_periodic() -> None:
    global _PERIODIC_TASK
    if _PERIODIC_TASK and not _PERIODIC_TASK.done():
        _PERIODIC_TASK.cancel()
        try:
            await _PERIODIC_TASK
        except (asyncio.CancelledError, Exception):
            pass
    _PERIODIC_TASK = None
