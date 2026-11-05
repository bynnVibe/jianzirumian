"""
见字如面 - 主问答语义缓存（按用户历史问答，语义相似问题命中直回、绕过检索路由）

针对【当前用户】的历史问答建立语义缓存：每次正常作答后把「问题向量 + 回答」写入缓存；
下次提问时先按语义相似度匹配该用户的历史问答，若相似度 ≥ 阈值则直接回复缓存内容，
不再走知识检索 / Agent / LLM 生成的业务路由，减少重复工作与上游资源消耗。

设计原则（对齐既有降级内核）：
- 独立开关 SEMANTIC_CACHE_ENABLED，可在系统设置页运行时覆盖；
- per-user 隔离：仅匹配当前用户自己的历史问答，不跨用户串答；
- 质量闸门：过短提问不参与匹配/写入，过短回答不缓存，避免污染；
- 失败静默降级：embedding/存储任何异常都只记日志，绝不阻塞问答主链路；
- 存储自包含：embedding 以 JSON 数组存 SQLite，内存余弦匹配（per-user 条目量小）。
"""
import asyncio
import json
import logging
import math
import re
import uuid
from datetime import datetime
from typing import List, Optional

from app.config import settings
from app.core import db

logger = logging.getLogger("jianziruyang.semantic_cache")


def semantic_cache_enabled() -> bool:
    """生效的语义缓存开关：系统设置页 > .env 默认"""
    from app.services.config_manager import runtime_config
    value = runtime_config.get("semantic_cache_enabled")
    return bool(settings.SEMANTIC_CACHE_ENABLED) if value is None else bool(value)


def normalize_question(text: str) -> str:
    """归一化提问文本，用于 UNIQUE(user_id, question_norm) 精确去重。

    去除首尾空白、压缩内部空白、转小写、剔除中英文标点，使「高血压吃什么？」与
    「高血压  吃什么」归一到同一 key。
    """
    norm = re.sub(r"[\s\u3000]+", "", (text or "").strip().lower())
    norm = re.sub(r"[，。！？、；：「」『』（）()【】\[\]<>《》,.!?;:\"'`~·…—\-_/\\|@#$%^&*+=]", "", norm)
    return norm


def _cosine(a: List[float], b: List[float]) -> float:
    """余弦相似度（纯函数）；维度不一致或零向量返回 0.0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def match_best(query_vec: List[float], entries: List[dict], threshold: float) -> Optional[dict]:
    """在候选条目中找出相似度最高且 ≥ 阈值者（纯函数，可离线测试）。

    entries: 每项含已解析的 'embedding'（list[float]）及其它字段；
    返回命中的 entry 副本（附 'score'），无命中返回 None。
    """
    best, best_score = None, 0.0
    for e in entries:
        score = _cosine(query_vec, e.get("embedding") or [])
        if score >= threshold and score > best_score:
            best, best_score = e, score
    if best is None:
        return None
    out = dict(best)
    out["score"] = round(best_score, 4)
    return out


async def _embed(text: str) -> List[float]:
    """经模型网关（role=embed）计算提问向量；同步 HTTP 放线程池，失败返回 []。"""
    try:
        from app.core.embeddings import EmbeddingFactory
        client = EmbeddingFactory.create_for_role("embed")
        vec = await asyncio.to_thread(client.embed_query, text)
        return list(vec) if vec else []
    except Exception as e:
        logger.warning("[语义缓存] embedding 失败，跳过: %s", e)
        return []


def _parse_row(row: dict) -> dict:
    """DB 行 → 内存条目（解析 sources/embedding JSON）。"""
    try:
        emb = json.loads(row.get("embedding") or "[]")
    except Exception:
        emb = []
    try:
        srcs = json.loads(row.get("sources") or "[]")
    except Exception:
        srcs = []
    return {
        "id": row.get("id"),
        "question": row.get("question"),
        "answer": row.get("answer"),
        "sources": srcs,
        "embedding": emb,
        "hit_count": row.get("hit_count", 0),
        "created_at": row.get("created_at"),
        "last_used_at": row.get("last_used_at"),
    }


def _valid_user(user_id: str) -> bool:
    return bool(user_id) and user_id not in ("", "guest")


async def lookup(user_id: str, question: str, threshold: Optional[float] = None) -> Optional[dict]:
    """按语义相似度查找当前用户历史问答缓存；命中则 bump 使用计数并返回条目，否则 None。

    门禁：开关关闭 / 游客 / 提问过短 → 直接返回 None（不匹配、不阻塞）。
    """
    if not semantic_cache_enabled() or not _valid_user(user_id):
        return None
    q = (question or "").strip()
    if len(q) < int(getattr(settings, "SEMANTIC_CACHE_MIN_QUESTION_CHARS", 4)):
        return None

    query_vec = await _embed(q)
    if not query_vec:
        return None

    rows = db.query(
        "SELECT id, question, answer, sources, embedding, hit_count, created_at, last_used_at"
        " FROM semantic_cache WHERE user_id = ?",
        (user_id,),
    )
    entries = [_parse_row(r) for r in rows]
    thr = float(threshold if threshold is not None else settings.SEMANTIC_CACHE_THRESHOLD)
    hit = match_best(query_vec, entries, thr)
    if not hit:
        return None

    now = datetime.now().isoformat()
    try:
        db.execute(
            "UPDATE semantic_cache SET hit_count = hit_count + 1, last_used_at = ?"
            " WHERE id = ? AND user_id = ?",
            (now, hit["id"], user_id),
        )
    except Exception as e:
        logger.warning("[语义缓存] 更新命中计数失败: %s", e)
    hit["hit_count"] = int(hit.get("hit_count", 0)) + 1
    hit["last_used_at"] = now
    logger.info("[语义缓存] 命中: user=%s score=%s q=%.30s", user_id, hit.get("score"), q)
    return hit


async def store(user_id: str, question: str, answer: str, sources: Optional[List[dict]] = None) -> bool:
    """把一次正常问答写入当前用户的语义缓存（按归一化问题去重 upsert，并做容量裁剪）。

    门禁：开关关闭 / 游客 / 提问或回答过短 → 跳过。失败静默返回 False，绝不抛出。
    """
    if not semantic_cache_enabled() or not _valid_user(user_id):
        return False
    q = (question or "").strip()
    a = (answer or "").strip()
    if len(q) < int(getattr(settings, "SEMANTIC_CACHE_MIN_QUESTION_CHARS", 4)):
        return False
    if len(a) < int(getattr(settings, "SEMANTIC_CACHE_MIN_ANSWER_CHARS", 30)):
        return False

    try:
        query_vec = await _embed(q)
        if not query_vec:
            return False
        now = datetime.now().isoformat()
        qnorm = normalize_question(q)
        srcs_json = json.dumps(sources or [], ensure_ascii=False)
        emb_json = json.dumps(query_vec)
        existing = db.query_one(
            "SELECT id FROM semantic_cache WHERE user_id = ? AND question_norm = ?",
            (user_id, qnorm),
        )
        if existing:
            db.execute(
                "UPDATE semantic_cache SET question = ?, answer = ?, sources = ?,"
                " embedding = ?, last_used_at = ? WHERE id = ?",
                (q, a, srcs_json, emb_json, now, existing["id"]),
            )
        else:
            db.execute(
                "INSERT INTO semantic_cache"
                " (id, user_id, question, question_norm, answer, sources, embedding,"
                "  hit_count, created_at, last_used_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (str(uuid.uuid4()), user_id, q, qnorm, a, srcs_json, emb_json, now, now),
            )
        _trim(user_id)
        return True
    except Exception as e:
        logger.warning("[语义缓存] 写入失败: %s", e)
        return False


def _trim(user_id: str):
    """容量裁剪：超出 MAX_ENTRIES 时按 last_used_at 淘汰最久未使用者。"""
    try:
        cap = int(getattr(settings, "SEMANTIC_CACHE_MAX_ENTRIES", 200))
        if cap <= 0:
            return
        n = db.query_one(
            "SELECT COUNT(*) AS n FROM semantic_cache WHERE user_id = ?", (user_id,)
        )
        total = int((n or {}).get("n", 0))
        if total <= cap:
            return
        stale = db.query(
            "SELECT id FROM semantic_cache WHERE user_id = ?"
            " ORDER BY last_used_at ASC LIMIT ?",
            (user_id, total - cap),
        )
        for r in stale:
            db.execute("DELETE FROM semantic_cache WHERE id = ? AND user_id = ?",
                       (r["id"], user_id))
    except Exception as e:
        logger.warning("[语义缓存] 容量裁剪失败: %s", e)


def list_entries(user_id: str, limit: int = 50) -> List[dict]:
    """列出当前用户的缓存条目（按最近使用倒序，不含 embedding）。"""
    if not _valid_user(user_id):
        return []
    rows = db.query(
        "SELECT id, question, answer, sources, hit_count, created_at, last_used_at"
        " FROM semantic_cache WHERE user_id = ? ORDER BY last_used_at DESC LIMIT ?",
        (user_id, int(limit)),
    )
    out = []
    for r in rows:
        e = _parse_row(r)
        e.pop("embedding", None)
        out.append(e)
    return out


def count(user_id: str) -> int:
    """当前用户缓存条目数。"""
    if not _valid_user(user_id):
        return 0
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM semantic_cache WHERE user_id = ?", (user_id,)
    )
    return int((row or {}).get("n", 0))


def delete_entry(user_id: str, entry_id: str) -> bool:
    """删除当前用户的单条缓存；返回是否删除。"""
    if not _valid_user(user_id) or not entry_id:
        return False
    try:
        n = db.execute(
            "DELETE FROM semantic_cache WHERE id = ? AND user_id = ?", (entry_id, user_id)
        )
        return bool(n)
    except Exception as e:
        logger.warning("[语义缓存] 删除条目失败: %s", e)
        return False


def clear(user_id: str) -> int:
    """清空当前用户的全部缓存；返回删除条数。"""
    if not _valid_user(user_id):
        return 0
    try:
        return int(db.execute("DELETE FROM semantic_cache WHERE user_id = ?", (user_id,)) or 0)
    except Exception as e:
        logger.warning("[语义缓存] 清空失败: %s", e)
        return 0


def stats(user_id: str) -> dict:
    """语义缓存面板概览：开关状态、阈值、条目数、累计命中。"""
    row = db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(hit_count), 0) AS hits"
        " FROM semantic_cache WHERE user_id = ?",
        (user_id,) if _valid_user(user_id) else ("__none__",),
    )
    return {
        "enabled": semantic_cache_enabled(),
        "threshold": float(settings.SEMANTIC_CACHE_THRESHOLD),
        "count": int((row or {}).get("n", 0)) if _valid_user(user_id) else 0,
        "total_hits": int((row or {}).get("hits", 0)) if _valid_user(user_id) else 0,
    }
