"""
见字如面 - 知识检索快照复用服务。

场景：用户先只开「知识检索」提问，回答末尾弹出能力受限卡，点击「开启联网搜索并重新提问」
后同一问题再走一轮。旧实现第二轮把知识检索（含重排序）完整重跑一遍，既慢又浪费 token。

本服务以 (session_id, user_id, 归一化问题) 为键保存上一轮知识检索产物：
- kb_results：向量库原始命中 [(text, meta, score), ...]，供 Agent 直接预填累积状态；
- sources / context_text：已重排并编号的知识上下文，复用后跳过 rerank，保证 [来源 X] 编号一致；
- steps：上一轮知识检索步骤说明（purpose/query/summary），第二轮作为「已完成·复用」待办呈现。

TTL 内有效（默认 15 分钟），过期/无快照一律回落正常检索，不改变既有行为。
游客不写快照：user_id 为共享值，避免跨访客串用检索结果。
"""
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from app.core import db

logger = logging.getLogger("jianziruyang.retrieval_reuse")

# 快照有效期：覆盖「看到能力受限卡 → 点击开启联网重发」的正常思考时间
SNAPSHOT_TTL_SECONDS = 15 * 60


def _now() -> str:
    return datetime.now().isoformat()


def query_key(query: str) -> str:
    """归一化问题指纹：去首尾空白、压缩连续空白、统一小写后取 SHA256。"""
    normalized = re.sub(r"\s+", " ", (query or "").strip()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _usable(session_id: str, user_id: str, query: str) -> bool:
    return bool(session_id) and bool(user_id) and user_id != "guest" and bool((query or "").strip())


def save(*, session_id: str, user_id: str, query: str, mode: str = "",
         kb_results: Optional[List] = None, sources: Optional[List[dict]] = None,
         context_text: str = "", steps: Optional[List[dict]] = None) -> bool:
    """写入/刷新快照（upsert，TTL 顺延）。无检索产物或游客不写，返回是否写入。"""
    if not _usable(session_id, user_id, query) or not kb_results:
        return False
    try:
        payload_results = json.dumps(
            [[text, meta or {}, float(score or 0)] for text, meta, score in kb_results],
            ensure_ascii=False)
        now = _now()
        expires = (datetime.now() + timedelta(seconds=SNAPSHOT_TTL_SECONDS)).isoformat()
        db.execute(
            "INSERT INTO chat_retrieval_snapshots (id, session_id, user_id, query_key, query, mode,"
            " kb_results, sources, context_text, steps, created_at, updated_at, expires_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(session_id, user_id, query_key) DO UPDATE SET"
            " query=excluded.query, mode=excluded.mode, kb_results=excluded.kb_results,"
            " sources=excluded.sources, context_text=excluded.context_text, steps=excluded.steps,"
            " updated_at=excluded.updated_at, expires_at=excluded.expires_at",
            (str(uuid.uuid4()), session_id, user_id, query_key(query), query or "", mode or "",
             payload_results, json.dumps(sources or [], ensure_ascii=False), context_text or "",
             json.dumps(steps or [], ensure_ascii=False), now, now, expires),
        )
        logger.info("[retrieval_reuse] 快照写入: session=%s user=%s mode=%s hits=%d sources=%d steps=%d ttl=%ds",
                    session_id, user_id, mode or "-", len(kb_results), len(sources or []),
                    len(steps or []), SNAPSHOT_TTL_SECONDS)
        return True
    except Exception as e:
        logger.warning("[retrieval_reuse] 快照写入失败（回落正常检索）: %s", e)
        return False


def load(session_id: str, user_id: str, query: str) -> Optional[dict]:
    """读取有效快照；过期/不存在返回 None（调用方据此回落正常检索）。"""
    if not _usable(session_id, user_id, query):
        return None
    try:
        row = db.query_one(
            "SELECT * FROM chat_retrieval_snapshots WHERE session_id=? AND user_id=? AND query_key=?",
            (session_id, user_id, query_key(query)))
        if not row:
            return None
        if (row["expires_at"] or "") <= _now():
            logger.info("[retrieval_reuse] 快照已过期，回落正常检索: session=%s", session_id)
            return None
        kb_results = []
        for item in json.loads(row["kb_results"] or "[]"):
            try:
                text, meta, score = item
            except (ValueError, TypeError):
                continue
            kb_results.append((text, meta or {}, float(score or 0)))
        if not kb_results:
            return None
        return {
            "session_id": row["session_id"], "user_id": row["user_id"], "query": row["query"],
            "mode": row["mode"] or "", "kb_results": kb_results,
            "sources": json.loads(row["sources"] or "[]"),
            "context_text": row["context_text"] or "",
            "steps": json.loads(row["steps"] or "[]"),
            "created_at": row["created_at"], "expires_at": row["expires_at"],
        }
    except Exception as e:
        logger.warning("[retrieval_reuse] 快照读取失败（回落正常检索）: %s", e)
        return None


def purge_expired() -> int:
    """惰性清理过期快照（写入/读取路径之外由定时任务或测试调用）。"""
    try:
        n = int(db.execute(
            "DELETE FROM chat_retrieval_snapshots WHERE expires_at != '' AND expires_at <= ?",
            (_now(),)) or 0)
        n += int(db.execute(
            "DELETE FROM chat_pending_plans WHERE expires_at != '' AND expires_at <= ?",
            (_now(),)) or 0)
        return n
    except Exception as e:
        logger.warning("[retrieval_reuse] 过期清理失败: %s", e)
        return 0


# ---------------------------------------------------------------------------
# 待授权计划（计划后、执行前的联网授权 HITL 续跑载体）
# ---------------------------------------------------------------------------

def save_pending_plan(*, session_id: str, user_id: str, query: str,
                      steps: List[dict], mode: str = "plan_execute") -> bool:
    """暂存已规划但未执行的步骤清单（含需授权的联网步骤），供授权后免重规划续跑。"""
    if not _usable(session_id, user_id, query) or not steps:
        return False
    try:
        now = _now()
        expires = (datetime.now() + timedelta(seconds=SNAPSHOT_TTL_SECONDS)).isoformat()
        db.execute(
            "INSERT INTO chat_pending_plans (id, session_id, user_id, query_key, query, mode,"
            " steps, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(session_id, user_id, query_key) DO UPDATE SET"
            " query=excluded.query, mode=excluded.mode, steps=excluded.steps,"
            " created_at=excluded.created_at, expires_at=excluded.expires_at",
            (str(uuid.uuid4()), session_id, user_id, query_key(query), query or "", mode or "",
             json.dumps(steps, ensure_ascii=False), now, expires),
        )
        logger.info("[retrieval_reuse] 待授权计划暂存: session=%s user=%s steps=%d",
                    session_id, user_id, len(steps))
        return True
    except Exception as e:
        logger.warning("[retrieval_reuse] 待授权计划暂存失败（授权后重新规划）: %s", e)
        return False


def take_pending_plan(session_id: str, user_id: str, query: str) -> Optional[List[dict]]:
    """取走待授权计划（读后即删，一次性）；过期/不存在返回 None → 重新规划。"""
    if not _usable(session_id, user_id, query):
        return None
    try:
        row = db.query_one(
            "SELECT * FROM chat_pending_plans WHERE session_id=? AND user_id=? AND query_key=?",
            (session_id, user_id, query_key(query)))
        if not row:
            return None
        db.execute("DELETE FROM chat_pending_plans WHERE id=?", (row["id"],))
        if (row["expires_at"] or "") <= _now():
            logger.info("[retrieval_reuse] 待授权计划已过期，重新规划: session=%s", session_id)
            return None
        steps = json.loads(row["steps"] or "[]")
        if not isinstance(steps, list) or not steps:
            return None
        logger.info("[retrieval_reuse] 续跑待授权计划: session=%s steps=%d", session_id, len(steps))
        return steps
    except Exception as e:
        logger.warning("[retrieval_reuse] 待授权计划读取失败（重新规划）: %s", e)
        return None