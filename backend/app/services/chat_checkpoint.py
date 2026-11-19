"""
见字如面 - 中断任务 checkpoint 服务（整改4）。

用户中途停止/整体超时时，chat_stream 外层把当前任务打包为 checkpoint：
- 界面显示「是否继续」：继续 → claim（pending→continued）并注入前序上下文补全；
- 不继续 → 软删除（pending→abandoned），记录保留 SQLite，Redis 副本 48h TTL；
- 创建/继续/放弃均写用户记忆与应用日志，防止用户后续想检索该任务时无据可查。
"""
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from app.core import db

logger = logging.getLogger("jianziruyang.chat_checkpoint")

# 软删除后的缓存保留期：48 小时
CHECKPOINT_TTL_SECONDS = 48 * 3600
_REDIS_PREFIX = "chat_checkpoint:"


def _now() -> str:
    return datetime.now().isoformat()


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"], "session_id": row["session_id"], "user_id": row["user_id"],
        "query": row["query"], "partial_answer": row["partial_answer"] or "",
        "sources": json.loads(row["sources"] or "[]"),
        "search_results": json.loads(row["search_results"] or "[]"),
        "user_message_id": row["user_message_id"] or "",
        "status": row["status"], "created_at": row["created_at"],
        "updated_at": row["updated_at"], "expires_at": row["expires_at"] or "",
    }


def _redis_set(cp_id: str, payload: dict, ttl: int = CHECKPOINT_TTL_SECONDS):
    """best-effort 写 Redis 副本（48h）；失败仅告警，SQLite 为事实源。"""
    try:
        from app.core import file_cache
        if not file_cache.effective_file_cache_enabled():
            return
        import redis
        client = redis.Redis.from_url(file_cache.effective_redis_url(), decode_responses=True,
                                      socket_timeout=2, socket_connect_timeout=2)
        client.set(_REDIS_PREFIX + cp_id, json.dumps(payload, ensure_ascii=False), ex=ttl)
    except Exception as e:
        logger.warning("[checkpoint] Redis 副本写入失败（SQLite 为事实源）: %s", e)


def _write_memory(user_id: str, session_id: str, content: str):
    """checkpoint 生命周期事件写入用户记忆（dedup=False，事件型不去重）。"""
    try:
        from app.services.memory import memory_service
        memory_service.add_memory(user_id, content, memory_type="fact", importance=0.4,
                                  source_session_id=session_id, dedup=False)
    except Exception as e:
        logger.warning("[checkpoint] 记忆写入失败: %s", e)


def create_checkpoint(*, session_id: str, user_id: str, query: str, partial_answer: str = "",
                      sources: Optional[List[dict]] = None, search_results: Optional[List[dict]] = None,
                      user_message_id: str = "", reason: str = "cancelled") -> Optional[dict]:
    """停止/超时即打包 checkpoint（pending），写记忆+日志+Redis 48h 副本。"""
    if not user_id or user_id == "guest":
        return None
    cp_id = str(uuid.uuid4())
    now = _now()
    db.execute(
        "INSERT INTO chat_checkpoints (id, session_id, user_id, query, partial_answer, sources,"
        " search_results, user_message_id, status, created_at, updated_at, expires_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (cp_id, session_id, user_id, query or "", partial_answer or "",
         json.dumps(sources or [], ensure_ascii=False),
         json.dumps(search_results or [], ensure_ascii=False),
         user_message_id or "", "pending", now, now, ""),
    )
    payload = _row_to_dict(db.query_one("SELECT * FROM chat_checkpoints WHERE id=?", (cp_id,)))
    _redis_set(cp_id, payload)
    logger.info("[checkpoint] 创建: id=%s session=%s user=%s reason=%s query=%.60s partial_chars=%d",
                cp_id, session_id, user_id, reason, query or "", len(partial_answer or ""))
    _write_memory(user_id, session_id,
                  f"用户于 {now} 中途停止任务（{reason}）：「{(query or '')[:80]}」。"
                  f"已保存 checkpoint {cp_id}（状态 pending，可继续或放弃）。"
                  f"已完成部分：{(partial_answer or '')[:200] or '尚未产出正文'}")
    return payload


def list_pending(session_id: str, user_id: str) -> List[dict]:
    """当前会话待处理的 checkpoint（界面「是否继续」条数据源）。"""
    purge_expired()
    rows = db.query(
        "SELECT * FROM chat_checkpoints WHERE session_id=? AND user_id=? AND status='pending'"
        " ORDER BY created_at DESC", (session_id, user_id))
    return [_row_to_dict(r) for r in rows]


def claim(cp_id: str, user_id: str, session_id: str = "") -> Optional[dict]:
    """继续任务：pending→continued（并发安全），返回 checkpoint 内容。"""
    row = db.query_one("SELECT * FROM chat_checkpoints WHERE id=? AND user_id=?", (cp_id, user_id))
    if not row or row["status"] != "pending":
        return None
    if session_id and row["session_id"] != session_id:
        return None
    n = db.execute("UPDATE chat_checkpoints SET status='continued', updated_at=? WHERE id=? AND status='pending'",
                   (_now(), cp_id))
    if not n:
        return None
    logger.info("[checkpoint] 继续: id=%s session=%s user=%s query=%.60s",
                cp_id, row["session_id"], user_id, row["query"])
    _write_memory(user_id, row["session_id"],
                  f"用户于 {_now()} 选择继续 checkpoint {cp_id}（任务「{row['query'][:80]}」），在前序进度基础上补全。")
    return dict(_row_to_dict(row), status="continued")


def abandon(cp_id: str, user_id: str) -> bool:
    """不继续：软删除 pending→abandoned，Redis 副本 48h TTL，写记忆+日志。"""
    row = db.query_one("SELECT * FROM chat_checkpoints WHERE id=? AND user_id=?", (cp_id, user_id))
    if not row or row["status"] != "pending":
        return False
    expires = (datetime.now() + timedelta(seconds=CHECKPOINT_TTL_SECONDS)).isoformat()
    n = db.execute(
        "UPDATE chat_checkpoints SET status='abandoned', expires_at=?, updated_at=? WHERE id=? AND status='pending'",
        (expires, _now(), cp_id))
    if not n:
        return False
    _redis_set(cp_id, dict(_row_to_dict(row), status="abandoned", expires_at=expires))
    logger.info("[checkpoint] 软删除（缓存保留48h）: id=%s user=%s expires=%s query=%.60s",
                cp_id, user_id, expires, row["query"])
    _write_memory(user_id, row["session_id"],
                  f"用户于 {_now()} 选择不继续 checkpoint {cp_id}（任务「{row['query'][:80]}」），"
                  f"已软删除，缓存保留 48h（至 {expires}）。后续如需检索该任务，可查本记忆与应用日志。")
    return True


def resume_context(cp: dict) -> str:
    """继续任务的 system 注入文本：原问题 + 已完成部分，要求补全不重复。"""
    partial = (cp.get("partial_answer") or "").strip()
    lines = [
        "用户此前中途停止了以下任务，本轮为继续任务：请在前序进度基础上补全与完善，"
        "不要重复已完成的内容，也不要要求用户重述问题。",
        f"原始问题：{cp.get('query') or ''}",
    ]
    if partial:
        lines.append(f"已完成部分（不完整回答）：\n{partial[:4000]}")
    else:
        lines.append("前序尚未产出回答正文（停止于检索/生成准备阶段），请完整回答原始问题。")
    srcs = cp.get("sources") or []
    if srcs:
        lines.append(f"前序已检索到 {len(srcs)} 份知识来源，可沿用同一知识上下文作答。")
    return "\n".join(lines)


def purge_expired() -> int:
    """惰性清理：软删除且超过 48h 保留期的 checkpoint 物理删除。"""
    try:
        return int(db.execute(
            "DELETE FROM chat_checkpoints WHERE status='abandoned' AND expires_at != '' AND expires_at <= ?",
            (_now(),)) or 0)
    except Exception as e:
        logger.warning("[checkpoint] 过期清理失败: %s", e)
        return 0