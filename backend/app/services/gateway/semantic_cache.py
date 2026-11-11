"""
见字如面 - 网关层语义缓存引擎（需求：构建在网关层 + 公共/个人域 + 实时/非实时阶梯 TTL）

本模块是语义缓存的唯一实现，由【模型网关代理】(services/gateway/proxy.py) 在
chat_answer/assistant 角色出网前调用：命中则直接合成响应、跳过上游 LLM；未命中则
上游成功返回后回写。业务层（chat.py）只负责在意图识别后计算「公共/个人 + 实时」
标签，经 extra_body 的 _gw_cache_* 字段传入网关，不在业务层直接读写缓存。

设计要点：
- 公共/个人域：scope='public' 的条目以哨兵 user_id='__public__' 存储 → 所有用户
  （含游客）均可复用；scope='personal' 按真实 user_id 隔离，仅本用户可见。
- 实时/非实时阶梯 TTL：实时类固定短 TTL（默认 2h）；非实时类按命中热度分档
  （冷 3 天 → 常命中 7 天 → 热点 30 天），命中即 bump 计数并刷新过期时间。
- 安全默认：标签解析失败/异常一律回落 personal，绝不把可能的个性化答案公共共享。
- 失败静默降级：embedding/存储任何异常都只记日志，绝不阻塞网关主链路。
"""
import asyncio
import json
import logging
import math
import re
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from app.config import settings
from app.core import db

logger = logging.getLogger("jianziruyang.gateway.semantic_cache")

# 公共域条目的哨兵属主：与任何真实 user_id 不冲突，UNIQUE(user_id, question_norm) 天然全局去重
PUBLIC_USER = "__public__"

# 个人数据信号：命中即强制 personal（安全网，避免把个性化答案误判为公共共享）
_PERSONAL_SIGNAL_RE = re.compile(
    r"我的|根据我|我上次|给我|替我|帮我|我为何|我最近|我自己|本人|我这种|适合我|我的记录|我的档案"
)


def semantic_cache_enabled() -> bool:
    """生效的语义缓存开关：系统设置页 > .env 默认"""
    from app.services.config_manager import runtime_config
    value = runtime_config.get("semantic_cache_enabled")
    return bool(settings.SEMANTIC_CACHE_ENABLED) if value is None else bool(value)


# 兼容别名（旧调用点/测试用 enabled 语义）
enabled = semantic_cache_enabled


def normalize_question(text: str) -> str:
    """归一化提问文本，用于 UNIQUE(user_id, question_norm) 精确去重。"""
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
    """在候选条目中找出相似度最高且 ≥ 阈值者（纯函数，可离线测试）。"""
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


# ---------------------------------------------------------------------------
# 标签与阶梯 TTL
# ---------------------------------------------------------------------------

def resolve_scope(llm_scope: str, query: str, *, uses_relevant_memory: bool = False) -> str:
    """综合判定缓存域（公共/个人）。

    判定优先级（安全优先，宁个人勿公共）：
      1. 问题含个人数据信号（我的/根据我…）或本轮注入了用户相关记忆 → personal；
      2. LLM 意图识别明确输出 public → public；
      3. 其余一律 personal（含解析失败/缺省）。
    """
    q = query or ""
    if _PERSONAL_SIGNAL_RE.search(q) or uses_relevant_memory:
        return "personal"
    return "public" if (llm_scope or "").strip().lower() == "public" else "personal"


def compute_ttl_seconds(is_realtime: bool, hit_count: int) -> int:
    """阶梯 TTL（秒）：实时固定短档；非实时按命中热度冷/常命中/热点三档升级。"""
    if is_realtime:
        return max(60, int(getattr(settings, "SEMANTIC_CACHE_TTL_REALTIME_HOURS", 2)) * 3600)
    hits = int(hit_count or 0)
    hot = int(getattr(settings, "SEMANTIC_CACHE_HOT_HITS", 10))
    warm = int(getattr(settings, "SEMANTIC_CACHE_WARM_HITS", 3))
    if hits >= hot:
        days = getattr(settings, "SEMANTIC_CACHE_TTL_HOT_DAYS", 30)
    elif hits >= warm:
        days = getattr(settings, "SEMANTIC_CACHE_TTL_WARM_DAYS", 7)
    else:
        days = getattr(settings, "SEMANTIC_CACHE_TTL_COLD_DAYS", 3)
    return max(60, int(days) * 86400)


def _expires_at(is_realtime: bool, hit_count: int) -> str:
    """按当前热度计算过期时间戳（ISO）。"""
    ttl = compute_ttl_seconds(is_realtime, hit_count)
    return (datetime.now() + timedelta(seconds=ttl)).isoformat()


def _is_expired(expires_at: str) -> bool:
    """空串=旧数据不过期（向后兼容）；否则按 ISO 时间比较。"""
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at) <= datetime.now()
    except Exception:
        return False


def ttl_label(is_realtime: bool, hit_count: int) -> str:
    """供面板展示的 TTL 档位中文标签。"""
    if is_realtime:
        return f"实时 {getattr(settings, 'SEMANTIC_CACHE_TTL_REALTIME_HOURS', 2)}h"
    hits = int(hit_count or 0)
    hot = int(getattr(settings, "SEMANTIC_CACHE_HOT_HITS", 10))
    warm = int(getattr(settings, "SEMANTIC_CACHE_WARM_HITS", 3))
    if hits >= hot:
        return f"热点 {getattr(settings, 'SEMANTIC_CACHE_TTL_HOT_DAYS', 30)}天"
    if hits >= warm:
        return f"常命中 {getattr(settings, 'SEMANTIC_CACHE_TTL_WARM_DAYS', 7)}天"
    return f"冷 {getattr(settings, 'SEMANTIC_CACHE_TTL_COLD_DAYS', 3)}天"


async def _embed(text: str) -> List[float]:
    """经模型网关（role=embed）计算提问向量；同步 HTTP 放线程池，失败返回 []。"""
    try:
        from app.core.embeddings import EmbeddingFactory
        client = EmbeddingFactory.create_for_role("embed")
        vec = await asyncio.to_thread(client.embed_query, text)
        return list(vec) if vec else []
    except Exception as e:
        logger.warning("[网关语义缓存] embedding 失败，跳过: %s", e)
        return []


def _valid_user(user_id: str) -> bool:
    return bool(user_id) and user_id not in ("", "guest")


def _parse_row(row: dict) -> dict:
    """DB 行 → 内存条目（解析 sources/embedding JSON，附带域/实时/过期字段）。"""
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
        "scope": row.get("scope") or "personal",
        "is_realtime": bool(row.get("is_realtime")),
        "expires_at": row.get("expires_at") or "",
        "expired": _is_expired(row.get("expires_at") or ""),
        "ttl_label": ttl_label(bool(row.get("is_realtime")), row.get("hit_count", 0)),
        "created_at": row.get("created_at"),
        "last_used_at": row.get("last_used_at"),
    }


# ---------------------------------------------------------------------------
# 命中查找 / 写入（网关代理调用）
# ---------------------------------------------------------------------------

async def lookup(user_id: str, question: str, threshold: Optional[float] = None,
                 *, scope: str = "personal", is_realtime: bool = False) -> Optional[dict]:
    """按语义相似度查找缓存；命中则 bump 计数、按热度刷新 TTL 并返回条目，否则 None。

    候选集：
      - scope='public'：本人个人条目 + 公共条目（公共域所有用户/游客共享复用）；
      - scope='personal'：仅本人个人条目（避免用通用公共答案回复个性化问题）。
    """
    if not semantic_cache_enabled():
        return None
    q = (question or "").strip()
    if len(q) < int(getattr(settings, "SEMANTIC_CACHE_MIN_QUESTION_CHARS", 4)):
        return None
    scope = "public" if scope == "public" else "personal"

    cols = ("id, question, answer, sources, embedding, hit_count, scope, is_realtime,"
            " expires_at, created_at, last_used_at")
    if scope == "public":
        rows = db.query(
            f"SELECT {cols} FROM semantic_cache WHERE user_id IN (?, ?)",
            (user_id or "", PUBLIC_USER),
        )
    else:
        if not _valid_user(user_id):
            return None
        rows = db.query(
            f"SELECT {cols} FROM semantic_cache WHERE user_id = ? AND scope = 'personal'",
            (user_id,),
        )

    entries = [e for e in (_parse_row(r) for r in rows) if not e["expired"]]
    if not entries:
        return None

    query_vec = await _embed(q)
    if not query_vec:
        return None
    thr = float(threshold if threshold is not None else settings.SEMANTIC_CACHE_THRESHOLD)
    hit = match_best(query_vec, entries, thr)
    if not hit:
        return None

    new_hits = int(hit.get("hit_count", 0)) + 1
    now_iso = datetime.now().isoformat()
    new_exp = _expires_at(bool(hit.get("is_realtime")), new_hits)
    try:
        db.execute(
            "UPDATE semantic_cache SET hit_count = ?, last_used_at = ?, expires_at = ? WHERE id = ?",
            (new_hits, now_iso, new_exp, hit["id"]),
        )
    except Exception as e:
        logger.warning("[网关语义缓存] 更新命中计数失败: %s", e)
    hit["hit_count"] = new_hits
    hit["last_used_at"] = now_iso
    hit["expires_at"] = new_exp
    hit["ttl_label"] = ttl_label(bool(hit.get("is_realtime")), new_hits)
    logger.info("[网关语义缓存] 命中 scope=%s user=%s score=%s q=%.30s",
                hit.get("scope"), user_id or "-", hit.get("score"), q)
    return hit


async def store(user_id: str, question: str, answer: str, sources: Optional[List[dict]] = None,
                *, scope: str = "personal", is_realtime: bool = False) -> bool:
    """把一次正常问答写入缓存（按归一化问题去重 upsert + 容量裁剪）。

    scope='public' → 属主=PUBLIC_USER（全体共享）；scope='personal' → 属主=user_id。
    门禁：开关关闭 / 提问或回答过短 / 个人域但游客 → 跳过。失败静默返回 False。
    """
    if not semantic_cache_enabled():
        return False
    q = (question or "").strip()
    a = (answer or "").strip()
    if len(q) < int(getattr(settings, "SEMANTIC_CACHE_MIN_QUESTION_CHARS", 4)):
        return False
    if len(a) < int(getattr(settings, "SEMANTIC_CACHE_MIN_ANSWER_CHARS", 30)):
        return False
    scope = "public" if scope == "public" else "personal"
    owner = PUBLIC_USER if scope == "public" else user_id
    if scope == "personal" and not _valid_user(user_id):
        return False

    try:
        query_vec = await _embed(q)
        if not query_vec:
            return False
        now = datetime.now().isoformat()
        qnorm = normalize_question(q)
        srcs_json = json.dumps(sources or [], ensure_ascii=False)
        emb_json = json.dumps(query_vec)
        rt = 1 if is_realtime else 0
        existing = db.query_one(
            "SELECT id, hit_count FROM semantic_cache WHERE user_id = ? AND question_norm = ?",
            (owner, qnorm),
        )
        if existing:
            hits = int(existing.get("hit_count", 0))
            exp = _expires_at(is_realtime, hits)  # 保留既有热度
            db.execute(
                "UPDATE semantic_cache SET question = ?, answer = ?, sources = ?, embedding = ?,"
                " scope = ?, is_realtime = ?, expires_at = ?, last_used_at = ? WHERE id = ?",
                (q, a, srcs_json, emb_json, scope, rt, exp, now, existing["id"]),
            )
        else:
            exp = _expires_at(is_realtime, 0)
            db.execute(
                "INSERT INTO semantic_cache"
                " (id, user_id, question, question_norm, answer, sources, embedding, hit_count,"
                "  scope, is_realtime, expires_at, created_at, last_used_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), owner, q, qnorm, a, srcs_json, emb_json,
                 scope, rt, exp, now, now),
            )
        _trim(owner)
        return True
    except Exception as e:
        logger.warning("[网关语义缓存] 写入失败: %s", e)
        return False


def _trim(owner_id: str):
    """容量裁剪：某属主条目超出 MAX_ENTRIES 时按 last_used_at 淘汰最久未使用者。"""
    try:
        cap = int(getattr(settings, "SEMANTIC_CACHE_MAX_ENTRIES", 200))
        if cap <= 0:
            return
        n = db.query_one(
            "SELECT COUNT(*) AS n FROM semantic_cache WHERE user_id = ?", (owner_id,)
        )
        total = int((n or {}).get("n", 0))
        if total <= cap:
            return
        stale = db.query(
            "SELECT id FROM semantic_cache WHERE user_id = ? ORDER BY last_used_at ASC LIMIT ?",
            (owner_id, total - cap),
        )
        for r in stale:
            db.execute("DELETE FROM semantic_cache WHERE id = ? AND user_id = ?",
                       (r["id"], owner_id))
    except Exception as e:
        logger.warning("[网关语义缓存] 容量裁剪失败: %s", e)


def purge_expired() -> int:
    """惰性清理：删除所有已过期条目（面板刷新/定时任务可调用）；返回删除条数。"""
    try:
        now = datetime.now().isoformat()
        return int(db.execute(
            "DELETE FROM semantic_cache WHERE expires_at != '' AND expires_at <= ?", (now,)
        ) or 0)
    except Exception as e:
        logger.warning("[网关语义缓存] 过期清理失败: %s", e)
        return 0


# ---------------------------------------------------------------------------
# 面板管理（api/chat.py 语义缓存面板）
# ---------------------------------------------------------------------------

def list_entries(user_id: str, limit: int = 50, *, include_public: bool = True) -> List[dict]:
    """列出缓存条目（本人个人域 + 可选公共域，按最近使用倒序，不含 embedding）。"""
    cols = ("id, question, answer, sources, hit_count, scope, is_realtime, expires_at,"
            " created_at, last_used_at")
    rows: List[dict] = []
    if _valid_user(user_id):
        rows += db.query(
            f"SELECT {cols} FROM semantic_cache WHERE user_id = ? ORDER BY last_used_at DESC LIMIT ?",
            (user_id, int(limit)),
        )
    if include_public:
        rows += db.query(
            f"SELECT {cols} FROM semantic_cache WHERE user_id = ? ORDER BY last_used_at DESC LIMIT ?",
            (PUBLIC_USER, int(limit)),
        )
    out = []
    seen = set()
    for r in rows:
        e = _parse_row(r)
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        e.pop("embedding", None)
        out.append(e)
    out.sort(key=lambda x: x.get("last_used_at") or "", reverse=True)
    return out[: int(limit)]


def count(user_id: str) -> int:
    """当前用户个人域缓存条目数。"""
    if not _valid_user(user_id):
        return 0
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM semantic_cache WHERE user_id = ?", (user_id,)
    )
    return int((row or {}).get("n", 0))


def delete_entry(user_id: str, entry_id: str) -> bool:
    """删除当前用户个人域的单条缓存；返回是否删除（公共域条目不允许普通用户删除）。"""
    if not _valid_user(user_id) or not entry_id:
        return False
    try:
        n = db.execute(
            "DELETE FROM semantic_cache WHERE id = ? AND user_id = ?", (entry_id, user_id)
        )
        return bool(n)
    except Exception as e:
        logger.warning("[网关语义缓存] 删除条目失败: %s", e)
        return False


def clear(user_id: str) -> int:
    """清空当前用户个人域的全部缓存；返回删除条数（不影响公共域）。"""
    if not _valid_user(user_id):
        return 0
    try:
        return int(db.execute("DELETE FROM semantic_cache WHERE user_id = ?", (user_id,)) or 0)
    except Exception as e:
        logger.warning("[网关语义缓存] 清空失败: %s", e)
        return 0


def stats(user_id: str) -> dict:
    """语义缓存面板概览：开关/阈值/本人条目数/公共条目数/累计命中。"""
    own = db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(hit_count), 0) AS hits"
        " FROM semantic_cache WHERE user_id = ?",
        (user_id,) if _valid_user(user_id) else ("__none__",),
    )
    pub = db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(hit_count), 0) AS hits"
        " FROM semantic_cache WHERE user_id = ?",
        (PUBLIC_USER,),
    )
    valid = _valid_user(user_id)
    return {
        "enabled": semantic_cache_enabled(),
        "threshold": float(settings.SEMANTIC_CACHE_THRESHOLD),
        "count": int((own or {}).get("n", 0)) if valid else 0,
        "total_hits": int((own or {}).get("hits", 0)) if valid else 0,
        "public_count": int((pub or {}).get("n", 0)),
        "public_hits": int((pub or {}).get("hits", 0)),
        "ttl": {
            "realtime_hours": int(getattr(settings, "SEMANTIC_CACHE_TTL_REALTIME_HOURS", 2)),
            "cold_days": int(getattr(settings, "SEMANTIC_CACHE_TTL_COLD_DAYS", 3)),
            "warm_days": int(getattr(settings, "SEMANTIC_CACHE_TTL_WARM_DAYS", 7)),
            "hot_days": int(getattr(settings, "SEMANTIC_CACHE_TTL_HOT_DAYS", 30)),
            "warm_hits": int(getattr(settings, "SEMANTIC_CACHE_WARM_HITS", 3)),
            "hot_hits": int(getattr(settings, "SEMANTIC_CACHE_HOT_HITS", 10)),
        },
    }
