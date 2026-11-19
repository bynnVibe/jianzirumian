"""
见字如面 - 网关层受控答案缓存唯一引擎。

业务层签发时效、上下文与证据合同，网关验证并绑定实际模型和上游。
只读写个人域 v2 表；旧条目、联网／工具／动态回答不参与复用。
有效期从生成时计算，命中仅增加热度，不延长新鲜度。
"""
import asyncio
import json
import logging
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from app.config import settings
from app.core import db
from app.services import cache_policy

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


def effective_threshold() -> float:
    """生效的命中阈值：系统设置页运行时覆盖 > .env 默认；clamp 0.5-0.99。

    整改：去除旧 0.95 硬下限——高相似改述提问（余弦约 0.90-0.95）应能命中。
    """
    from app.services.config_manager import runtime_config
    value = runtime_config.get("semantic_cache_threshold")
    raw = settings.SEMANTIC_CACHE_THRESHOLD if value is None else value
    try:
        t = float(raw)
    except (TypeError, ValueError):
        t = float(settings.SEMANTIC_CACHE_THRESHOLD)
    return min(0.99, max(0.5, t))


def normalize_question(text: str) -> str:
    """归一化提问文本，用于 UNIQUE(user_id, question_norm) 精确去重。"""
    return cache_policy.normalize_question(text)


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

def resolve_scope(llm_scope: str, query: str, *, uses_relevant_memory: bool = False,
                  uses_personal_kb: bool = False) -> str:
    """综合判定缓存域（公共/个人）。

    判定优先级（安全优先，宁个人勿公共）：
      1. 本轮回答引用了非公共知识库（个人库）内容 → personal（防跨用户泄露个人 KB 答案）；
      2. 问题含个人数据信号（我的/根据我…）或本轮注入了用户相关记忆 → personal；
      3. LLM 意图识别明确输出 public → public；
      4. 其余一律 personal（含解析失败/缺省）。
    """
    return "personal"


def compute_ttl_seconds(is_realtime: bool, hit_count: int) -> int:
    """阶梯 TTL（秒）：实时固定短档；非实时按命中热度冷/常命中/热点三档升级。"""
    return 0 if is_realtime else min(86400, max(1, settings.ANSWER_CACHE_TTL_SECONDS))


def _expires_at(is_realtime: bool, hit_count: int) -> str:
    """按当前热度计算过期时间戳（ISO）。"""
    ttl = compute_ttl_seconds(is_realtime, hit_count)
    return (datetime.now() + timedelta(seconds=ttl)).isoformat()


def _is_expired(expires_at: str) -> bool:
    """空串=旧数据不过期（向后兼容）；否则按 ISO 时间比较。"""
    if not expires_at:
        return True
    try:
        expiry = datetime.fromisoformat(expires_at)
        now = datetime.now(expiry.tzinfo) if expiry.tzinfo else datetime.now()
        return expiry <= now
    except (ValueError, TypeError):
        return True


def ttl_label(is_realtime: bool, hit_count: int) -> str:
    """供面板展示的 TTL 档位中文标签。"""
    return "实时问题不缓存答案" if is_realtime else f"硬期限 {compute_ttl_seconds(False, 0) / 3600:g} 小时（命中不续期）"


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
    try:
        revs = json.loads(row.get("revisions") or "{}")
    except Exception:
        revs = {}
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
        "cache_kind": row.get("cache_kind", ""),
        "evidence_hash": row.get("evidence_hash", ""),
        "revisions": revs,
        "model": row.get("model", ""),
        "profile_hash": row.get("profile_hash", ""),
    }


# ---------------------------------------------------------------------------
# 命中查找 / 写入（网关代理调用）
# ---------------------------------------------------------------------------

def _eligible(user_id: str, contract: dict, fingerprint: str) -> bool:
    return bool(semantic_cache_enabled() and _valid_user(user_id) and user_id != PUBLIC_USER
                and contract and contract.get("owner") == user_id and fingerprint
                and contract.get("decision", {}).get("kind") in ("knowledge_semantic", "model_exact")
                and contract.get("revisions") == cache_policy.revisions())


async def lookup(user_id: str, question: str, threshold: Optional[float] = None,
                 *, scope: str = "personal", is_realtime: bool = False,
                 contract: Optional[dict] = None, fingerprint: str = "") -> Optional[dict]:
    """只有验证过的新版合同参与个人域查询；命中不续期。"""
    if is_realtime or scope != "personal" or not _eligible(user_id, contract, fingerprint):
        return None
    if len(question.strip()) < settings.SEMANTIC_CACHE_MIN_QUESTION_CHARS:
        return None
    rows = db.query("SELECT * FROM semantic_cache_v2 WHERE user_id=? AND fingerprint=?",
                    (user_id, fingerprint))
    # 整改：条目有效性改由「知识库版本 revisions 相等 + 自身证据完整性」保证，
    # 不再要求与本轮检索证据快照严格相等（检索排序差异不应使缓存失效）。
    entries = [e for e in (_parse_row(r) for r in rows)
               if not e["expired"] and e["revisions"] == contract["revisions"]
               and cache_policy.digest(e["sources"]) == e["evidence_hash"]]
    if rows and not entries:
        record_event(user_id=user_id, hit=False, outcome="invalidated", reason="expired")
    hit = next((dict(e, score=1.0) for e in entries
                if normalize_question(e["question"]) == normalize_question(question)), None)
    if not hit and contract["decision"]["kind"] == "knowledge_semantic" and entries:
        vec = await _embed(question)
        thr = float(threshold) if threshold is not None else effective_threshold()
        hit = match_best(vec, entries, thr)
        if hit and not await cache_policy.equivalent(question, hit["question"], user_id):
            hit = None
    if not hit or not _eligible(user_id, contract, fingerprint):
        return None
    now = datetime.now(timezone.utc).isoformat()
    # 最后一次数据库内检查避免校验期间的并发过期／清除。
    n = db.execute("UPDATE semantic_cache_v2 SET hit_count=hit_count+1,last_used_at=? "
                   "WHERE id=? AND user_id=? AND expires_at>?", (now, hit["id"], user_id, now))
    if not n:
        return None
    hit["hit_count"] += 1
    hit["last_used_at"] = now
    return hit


async def probe(user_id: str, question: str, *, profile_hash: str = "") -> Optional[dict]:
    """检索前探针（整改2 核心）：在检索/生成之前查个人域知识语义缓存。

    命中则业务层直接复用缓存答案，跳过检索与上游 LLM，避免高相似重复提问
    重复消耗检索/生成资源。不依赖合同指纹（跨轮历史/证据差异不影响命中）；
    安全门禁：开关/合法用户/最小长度/个人化与指代信号/实时类一律旁路；
    条目有效性 = 未过期 + revisions 与当前知识库版本相等 + 自身证据完整
    + 常驻画像摘要一致（画像变更后旧条目不再命中）。
    """
    if not semantic_cache_enabled() or not _valid_user(user_id):
        return None
    q = (question or "").strip()
    if len(q) < settings.SEMANTIC_CACHE_MIN_QUESTION_CHARS:
        return None
    if cache_policy._PERSONAL.search(q) or cache_policy._REFERENCE.search(q):
        return None
    if cache_policy.detect_realtime(q):
        return None
    try:
        revs = cache_policy.revisions()
    except Exception:
        return None
    rows = db.query("SELECT * FROM semantic_cache_v2 WHERE user_id=? AND cache_kind='knowledge_semantic'",
                    (user_id,))
    entries = [e for e in (_parse_row(r) for r in rows)
               if not e["expired"] and e["revisions"] == revs
               and cache_policy.digest(e["sources"]) == e["evidence_hash"]
               and e.get("profile_hash", "") == profile_hash]
    if not entries:
        return None
    hit = next((dict(e, score=1.0) for e in entries
                if normalize_question(e["question"]) == normalize_question(q)), None)
    if not hit:
        vec = await _embed(q)
        if not vec:
            return None
        hit = match_best(vec, entries, effective_threshold())
        if hit and not await cache_policy.equivalent(q, hit["question"], user_id, score=hit.get("score")):
            hit = None
    if not hit:
        return None
    now = datetime.now(timezone.utc).isoformat()
    n = db.execute("UPDATE semantic_cache_v2 SET hit_count=hit_count+1,last_used_at=? "
                   "WHERE id=? AND user_id=? AND expires_at>?", (now, hit["id"], user_id, now))
    if not n:
        return None
    hit["hit_count"] += 1
    hit["last_used_at"] = now
    record_event(user_id=user_id, hit=True, entry_id=hit["id"], score=hit.get("score"),
                 question=q, outcome="hit")
    logger.info("[网关语义缓存] 检索前探针命中: user=%s score=%s question=%s",
                user_id, hit.get("score"), q[:60])
    return hit


async def store(user_id: str, question: str, answer: str, sources: Optional[List[dict]] = None,
                *, scope: str = "personal", is_realtime: bool = False,
                contract: Optional[dict] = None, fingerprint: str = "", upstream_id: str = "") -> bool:
    """正常完整答案原子写入 v2；无合同的旧调用不能复活旧缓存。"""
    if is_realtime or scope != "personal" or not _eligible(user_id, contract, fingerprint):
        return False
    q, a = question.strip(), answer.strip()
    if len(q) < settings.SEMANTIC_CACHE_MIN_QUESTION_CHARS or len(a) < settings.SEMANTIC_CACHE_MIN_ANSWER_CHARS:
        return False
    from app.services.capability_gap import answer_indicates_failure
    if answer_indicates_failure(a):
        return False
    references = [int(n) for n in re.findall(r"\[来源\s*(\d+)\]", a)]
    if any(n < 1 or n > len(contract["sources"]) for n in references):
        return False
    try:
        vec = await _embed(q) if contract["decision"]["kind"] == "knowledge_semantic" else []
        if contract["decision"]["kind"] == "knowledge_semantic" and not vec:
            return False
        if not _eligible(user_id, contract, fingerprint):
            return False
        now = datetime.now(timezone.utc)
        ttl = min(compute_ttl_seconds(False, 0), int(contract["decision"]["ttl_seconds"]))
        if ttl <= 0:
            return False
        db.execute(
            "INSERT INTO semantic_cache_v2 (id,user_id,question,question_norm,answer,sources,embedding,"
            "cache_kind,fingerprint,evidence_hash,revisions,model,upstream_id,profile_hash,expires_at,created_at,last_used_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id,fingerprint,question_norm) DO UPDATE SET "
            "answer=excluded.answer,sources=excluded.sources,embedding=excluded.embedding,"
            "expires_at=excluded.expires_at,created_at=excluded.created_at,last_used_at=excluded.last_used_at",
            (str(uuid.uuid4()), user_id, q, normalize_question(q), a,
             cache_policy.canonical(contract["sources"]), json.dumps(vec), contract["decision"]["kind"],
             fingerprint, contract["evidence_hash"], cache_policy.canonical(contract["revisions"]),
             contract["model"], upstream_id, contract.get("profile_hash", ""),
             (now + timedelta(seconds=ttl)).isoformat(), now.isoformat(), now.isoformat()),
        )
        _trim(user_id)
        return True
    except Exception as e:
        logger.warning("[网关语义缓存] v2 写入失败: %s", e)
        return False


def _trim(owner_id: str):
    """容量裁剪：某属主条目超出 MAX_ENTRIES 时按 last_used_at 淘汰最久未使用者。"""
    try:
        cap = int(getattr(settings, "SEMANTIC_CACHE_MAX_ENTRIES", 200))
        if cap <= 0:
            return
        n = db.query_one(
            "SELECT COUNT(*) AS n FROM semantic_cache_v2 WHERE user_id = ?", (owner_id,)
        )
        total = int((n or {}).get("n", 0))
        if total <= cap:
            return
        stale = db.query(
            "SELECT id FROM semantic_cache_v2 WHERE user_id = ? ORDER BY last_used_at ASC LIMIT ?",
            (owner_id, total - cap),
        )
        for r in stale:
            db.execute("DELETE FROM semantic_cache_v2 WHERE id = ? AND user_id = ?",
                       (r["id"], owner_id))
    except Exception as e:
        logger.warning("[网关语义缓存] 容量裁剪失败: %s", e)


def purge_expired() -> int:
    """惰性清理：删除所有已过期条目（面板刷新/定时任务可调用）；返回删除条数。"""
    try:
        now = datetime.now(timezone.utc).isoformat()
        return int(db.execute(
            "DELETE FROM semantic_cache_v2 WHERE expires_at != '' AND expires_at <= ?", (now,)
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
            " created_at, last_used_at, cache_kind, evidence_hash, model")
    rows: List[dict] = []
    if _valid_user(user_id):
        rows += db.query(
            f"SELECT {cols} FROM semantic_cache_v2 WHERE user_id = ? ORDER BY last_used_at DESC LIMIT ?",
            (user_id, int(limit)),
        )
    # v2 不开放跨用户公共答案；兼容参数仅保留调用签名。
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
        "SELECT COUNT(*) AS n FROM semantic_cache_v2 WHERE user_id = ?", (user_id,)
    )
    return int((row or {}).get("n", 0))


def delete_entry(user_id: str, entry_id: str) -> bool:
    """删除当前用户个人域的单条缓存；返回是否删除（公共域条目不允许普通用户删除）。"""
    if not _valid_user(user_id) or not entry_id:
        return False
    try:
        n = db.execute(
            "DELETE FROM semantic_cache_v2 WHERE id = ? AND user_id = ?", (entry_id, user_id)
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
        return int(db.execute("DELETE FROM semantic_cache_v2 WHERE user_id = ?", (user_id,)) or 0)
    except Exception as e:
        logger.warning("[网关语义缓存] 清空失败: %s", e)
        return 0


# ---------------------------------------------------------------------------
# 监控埋点与聚合（可观测性业务指标：命中率/节省/空间/语义详情）
# ---------------------------------------------------------------------------

def record_event(*, user_id: str = "", scope: str = "personal", hit: bool,
                 entry_id: str = "", score: Optional[float] = None,
                 latency_ms: int = 0, est_tokens: int = 0, saved_tokens: int = 0,
                 question: str = "", outcome: str = "", reason: str = "",
                 validation_ms: int = 0) -> Optional[str]:
    """记录一次缓存命中/未命中事件（失败静默不阻断主链路）。

    返回事件 id：miss 事件供上游成功后按真实 usage 补记 est_tokens。
    """
    try:
        from app.core.log_setup import redact_log_text
        eid = str(uuid.uuid4())
        db.execute(
            "INSERT INTO semantic_cache_events (id, created_at, user_id, scope, hit,"
            " entry_id, score, latency_ms, est_tokens, saved_tokens, question,outcome,reason,validation_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, datetime.now().isoformat(), user_id or "", "personal",
             1 if hit else 0, entry_id or "", float(score) if score is not None else 0.0,
             int(latency_ms or 0), int(est_tokens or 0), int(saved_tokens or 0),
             redact_log_text((question or "")[:120]), outcome or ("hit" if hit else "miss"),
             reason, int(validation_ms or 0)))
        return eid
    except Exception as e:
        logger.warning("[网关语义缓存] 监控事件记录失败: %s", e)
        return None


def update_event_tokens(event_id: str, est_tokens: int) -> None:
    """上游成功后按本次真实 usage 补记 miss 事件的 est_tokens（失败静默）。"""
    if not event_id:
        return
    try:
        db.execute("UPDATE semantic_cache_events SET est_tokens = ? WHERE id = ?",
                   (int(est_tokens or 0), event_id))
    except Exception as e:
        logger.warning("[网关语义缓存] 事件 Token 补记失败: %s", e)


def monitor_stats(days: int = 7) -> dict:
    """语义缓存监控聚合（require_ops 面板）。

    口径：
    - 命中率 = 命中事件 / (命中+未命中)；命中响应时间取事件 latency_ms（查找开销）；
    - 未命中对照 = 同窗口 gateway_logs 真实出网成功请求（total_tokens>0 排除缓存合成响应）；
    - 节省时间 = Σ(未命中均值 − 命中时延)；节省 Token = Σ saved_tokens；
    - 空间占用 = 条目 question+answer+sources+embedding 字节求和（MB）；
    - 语义详情返回前经 redact_log_text 脱敏（PII 单一事实源）。
    """
    from app.core.log_setup import redact_log_text
    days = max(1, int(days or 7))
    since = (datetime.now() - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat()
    ev = db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(hit), 0) AS hits,"
        " AVG(CASE WHEN hit = 1 THEN latency_ms END) AS hit_latency,"
        " COALESCE(SUM(saved_tokens), 0) AS saved_tokens"
        " FROM semantic_cache_events WHERE created_at >= ? AND outcome IN ('hit','miss')", (since,)) or {}
    total_ev = int(ev.get("n") or 0)
    hits = int(ev.get("hits") or 0)
    hit_latency = float(ev.get("hit_latency") or 0)
    miss = db.query_one(
        "SELECT COUNT(*) AS n, AVG(latency_ms) AS latency, AVG(total_tokens) AS tokens"
        " FROM gateway_logs WHERE created_at >= ? AND status = 'ok' AND total_tokens > 0",
        (since,)) or {}
    miss_latency = float(miss.get("latency") or 0)
    miss_tokens = float(miss.get("tokens") or 0)
    space = db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(question) + LENGTH(answer)"
        " + LENGTH(sources) + LENGTH(embedding)), 0) AS bytes FROM semantic_cache_v2") or {}
    pub = db.query_one(
        "SELECT COUNT(*) AS n FROM semantic_cache_v2 WHERE user_id = ?", (PUBLIC_USER,)) or {}
    expired = db.query_one(
        "SELECT COUNT(*) AS n FROM semantic_cache_v2 WHERE expires_at != '' AND expires_at <= ?",
        (datetime.now(timezone.utc).isoformat(),)) or {}
    daily_rows = db.query(
        "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n, COALESCE(SUM(hit), 0) AS hits,"
        " COALESCE(SUM(saved_tokens), 0) AS saved_tokens FROM semantic_cache_events"
        " WHERE created_at >= ? AND outcome IN ('hit','miss') GROUP BY day ORDER BY day", (since,))
    daily = [{"day": r["day"], "hits": int(r["hits"]), "misses": int(r["n"]) - int(r["hits"]),
              "hit_rate": round(int(r["hits"]) / int(r["n"]), 4) if r["n"] else 0.0,
              "saved_tokens": int(r["saved_tokens"])} for r in daily_rows]
    entries = []
    for r in db.query(
            "SELECT id, question, answer, hit_count, scope, is_realtime, expires_at, last_used_at,"
            " created_at,cache_kind,evidence_hash,model FROM semantic_cache_v2"
            " ORDER BY hit_count DESC, last_used_at DESC LIMIT 50"):
        entries.append({
            "id": r["id"],
            "question": redact_log_text((r.get("question") or "").replace("\n", " ")[:80]),
            "answer": redact_log_text((r.get("answer") or "").replace("\n", " ")[:160]),
            "hit_count": int(r.get("hit_count") or 0),
            "scope": r.get("scope") or "personal",
            "ttl_label": ttl_label(bool(r.get("is_realtime")), r.get("hit_count") or 0),
            "expired": _is_expired(r.get("expires_at") or ""),
            "last_used_at": (r.get("last_used_at") or "")[:16],
            "created_at": r.get("created_at"), "expires_at": r.get("expires_at"),
            "cache_kind": r.get("cache_kind"), "evidence_hash": r.get("evidence_hash"),
            "model": r.get("model"),
        })
    n_space = int(space.get("n") or 0)
    reasons = db.query("SELECT outcome,reason,COUNT(*) AS count FROM semantic_cache_events "
                       "WHERE created_at>=? AND outcome IN ('bypass','invalidated') GROUP BY outcome,reason", (since,))
    bypasses = sum(r["count"] for r in reasons if r["outcome"] == "bypass")
    return {
        "policy_version": 2, "reasons": reasons,
        "days": days,
        "enabled": semantic_cache_enabled(),
        "overview": {
            "requests": total_ev, "hits": hits, "misses": total_ev - hits,
            "bypasses": bypasses, "bypass_rate": round(bypasses / max(1, total_ev + bypasses), 4),
            "savings_are_estimates": True,
            "hit_rate": round(hits / total_ev, 4) if total_ev else 0.0,
            "saved_time_ms": int(hits * max(0.0, miss_latency - hit_latency)),
            "saved_tokens": int(ev.get("saved_tokens") or 0),
        },
        "comparison": {
            "hit_avg_latency_ms": round(hit_latency, 1),
            "miss_avg_latency_ms": round(miss_latency, 1),
            "hit_tokens": 0,
            "miss_avg_tokens": round(miss_tokens, 1),
            "miss_samples": int(miss.get("n") or 0),
        },
        "space": {
            "entries": n_space,
            "bytes": int(space.get("bytes") or 0),
            "mb": round(int(space.get("bytes") or 0) / 1048576, 3),
            "public_entries": int(pub.get("n") or 0),
            "personal_entries": max(0, n_space - int(pub.get("n") or 0)),
            "expired_entries": int(expired.get("n") or 0),
        },
        "daily": daily,
        "entries": entries,
    }


def stats(user_id: str) -> dict:
    """语义缓存面板概览：开关/阈值/本人条目数/公共条目数/累计命中。"""
    own = db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(hit_count), 0) AS hits"
        " FROM semantic_cache_v2 WHERE user_id = ?",
        (user_id,) if _valid_user(user_id) else ("__none__",),
    )
    pub = db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(hit_count), 0) AS hits"
        " FROM semantic_cache_v2 WHERE user_id = ?",
        (PUBLIC_USER,),
    )
    valid = _valid_user(user_id)
    return {
        "enabled": semantic_cache_enabled(),
        "threshold": effective_threshold(),
        "policy_version": 2, "signing_ready": bool(cache_policy.signing_key()),
        "public_enabled": False, "hard_ttl_seconds": compute_ttl_seconds(False, 0),
        "count": int((own or {}).get("n", 0)) if valid else 0,
        "total_hits": int((own or {}).get("hits", 0)) if valid else 0,
        "public_count": int((pub or {}).get("n", 0)),
        "public_hits": int((pub or {}).get("hits", 0)),
        "ttl": {
            "realtime_hours": 0, "cold_days": 1, "warm_days": 1, "hot_days": 1,
            "sliding": False,
        },
    }
