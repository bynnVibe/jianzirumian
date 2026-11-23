"""
见字如面 - 在线采样评测（DeepEval 风格质量指标回流线上监控）

对线上真实问答按采样率抽样，构造 LLMTestCase 跑 DeepEval 风格指标
（有检索上下文：Faithfulness + GEval；无上下文：仅 GEval），结果落
online_eval_results，回流可观测性质量指标与告警分析引擎，形成
「监控 → 评测 → 归因」闭环。

设计要点：
- 后台周期任务（lifespan 启停），游标存 runtime_config，重启不重复评测；
- 采样率/单轮上限可配；LLM 失败静默跳过，绝不阻断聊天主链路；
- 评测独立于线上会话：不写 chat_sessions/chat_messages、不计 usage；
- 原始文本落库不改写，API 返回前经 redact_log_text 脱敏（单一事实源）。
"""
import asyncio
import json
import logging
import random
import uuid
from datetime import datetime
from typing import List, Optional

from app.config import settings
from app.core import call_context
from app.core import db

logger = logging.getLogger("jianziruyang.online_eval")

_PERIODIC_TASK: Optional[asyncio.Task] = None
_CURSOR_KEY = "online_eval_cursor"


def _now() -> str:
    return datetime.now().isoformat()


def online_eval_enabled() -> bool:
    """生效开关：系统设置页运行时覆盖 > .env 默认"""
    from app.services.config_manager import runtime_config
    value = runtime_config.get("online_eval_enabled")
    return bool(settings.ONLINE_EVAL_ENABLED) if value is None else bool(value)


def _source_texts(sources_json: str) -> List[str]:
    """从消息 sources JSON 提取检索片段文本（在线评测的上下文载体）。"""
    try:
        sources = json.loads(sources_json or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    texts = []
    for s in sources if isinstance(sources, list) else []:
        if not isinstance(s, dict):
            continue
        chunks = s.get("chunks") or [s]
        for c in chunks:
            t = (c.get("text") if isinstance(c, dict) else "") or ""
            if t.strip():
                texts.append(t.strip()[:800])
    return texts[:8]


def _pick_candidates(cursor: str, batch: int = 50) -> List[dict]:
    """游标之后的新助手消息（排除系统/空回答），按时间升序取一批。"""
    where = "m.role = 'assistant' AND m.content != ''"
    params: list = []
    if cursor:
        where += " AND m.created_at > ?"
        params.append(cursor)
    return db.query(
        f"SELECT m.message_id, m.session_id, m.content, m.sources, m.created_at,"
        f" COALESCE(s.user_id, '') AS user_id FROM chat_messages m"
        f" LEFT JOIN chat_sessions s ON s.session_id = m.session_id"
        f" WHERE {where} ORDER BY m.created_at ASC LIMIT ?", (*params, batch))


def _question_for(msg: dict) -> str:
    """取该助手消息之前最近一条用户消息作为评测 input。"""
    row = db.query_one(
        "SELECT content FROM chat_messages WHERE session_id = ? AND role = 'user'"
        " AND seq < (SELECT seq FROM chat_messages WHERE message_id = ?)"
        " ORDER BY seq DESC LIMIT 1",
        (msg["session_id"], msg["message_id"]))
    return (row or {}).get("content") or ""


async def _eval_one(msg: dict) -> Optional[dict]:
    """对单条助手消息跑指标并落库；失败静默返回 None。"""
    from app.services import deepeval_metrics as dm

    question = _question_for(msg)
    answer = msg.get("content") or ""
    contexts = _source_texts(msg.get("sources") or "[]")
    test_case = dm.LLMTestCase(
        input=question, actual_output=answer, retrieval_context=contexts)
    metrics = ["faithfulness", "geval"] if contexts else ["geval"]
    try:
        measured = await dm.evaluate_case(
            test_case, metrics, settings.ONLINE_EVAL_GEVAL_CRITERIA)
    except Exception as e:
        logger.warning("[在线评测] 指标执行失败 msg=%s: %s", msg.get("message_id"), e)
        return None
    faith = (measured.get("faithfulness") or (None, {}))[0]
    geval_score, geval_detail = measured.get("geval") or (None, {})
    row_id = str(uuid.uuid4())
    try:
        db.execute(
            "INSERT INTO online_eval_results (id, created_at, session_id, message_id, user_id,"
            " question, answer, context_chars, faithfulness, geval_score, geval_reason,"
            " metric_details, status, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'ok', '')",
            (row_id, _now(), msg.get("session_id") or "", msg.get("message_id") or "",
             str(msg.get("user_id") or ""), question[:2000], answer[:4000],
             sum(len(c) for c in contexts), faith, geval_score,
             (geval_detail or {}).get("reason", "")[:500],
             json.dumps({k: v[1] for k, v in measured.items()}, ensure_ascii=False)[:8000]))
    except Exception as e:
        logger.warning("[在线评测] 结果落库失败: %s", e)
        return None
    return {"id": row_id, "faithfulness": faith, "geval_score": geval_score}


async def run_round() -> dict:
    """执行一轮采样评测（周期任务与测试共用入口）；返回本轮统计。"""
    from app.services.config_manager import runtime_config

    if not online_eval_enabled():
        return {"enabled": False, "evaluated": 0}
    cursor = str(runtime_config.get(_CURSOR_KEY) or "")
    candidates = await asyncio.to_thread(_pick_candidates, cursor)
    if not candidates:
        return {"enabled": True, "evaluated": 0, "candidates": 0}
    rate = max(0.0, min(1.0, float(settings.ONLINE_EVAL_SAMPLE_RATE)))
    cap = max(1, int(settings.ONLINE_EVAL_MAX_PER_ROUND))
    sampled = [m for m in candidates if random.random() < rate][:cap]
    evaluated = 0
    for msg in sampled:
        # 裁判 LLM 出网归属被评测问答的发起用户（周期任务无请求上下文），
        # 计入该用户绑定密钥而非系统内部密钥
        uid = str(msg.get("user_id") or "")
        ctx_token = call_context.activate(
            actor_user_id=uid, origin="background",
            job_id=str(msg.get("message_id") or "")) if uid else None
        try:
            if await _eval_one(msg):
                evaluated += 1
        except Exception as e:
            logger.warning("[在线评测] 单条评测异常（跳过）: %s", e)
        finally:
            if ctx_token is not None:
                call_context.deactivate(ctx_token)
    # 游标推进到本批最大 created_at（无论是否被采样，避免重复扫描）
    new_cursor = max(m.get("created_at") or "" for m in candidates)
    if new_cursor > cursor:
        runtime_config.set(_CURSOR_KEY, new_cursor)
    logger.info("[在线评测] 本轮候选 %d 条，采样评测 %d 条", len(candidates), evaluated)
    return {"enabled": True, "candidates": len(candidates), "evaluated": evaluated}


def aggregate(days: int = 7) -> dict:
    """近 N 天在线评测聚合（质量指标回流 + 告警规则消费）。"""
    from datetime import timedelta
    since = (datetime.now() - timedelta(days=max(1, days) - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat()
    row = db.query_one(
        "SELECT COUNT(*) AS n,"
        " AVG(faithfulness) AS avg_faith, AVG(geval_score) AS avg_geval,"
        " COALESCE(SUM(geval_score < 0.6), 0) AS low"
        " FROM online_eval_results WHERE created_at >= ? AND status = 'ok'", (since,)) or {}
    n = int(row.get("n") or 0)
    return {
        "samples": n,
        "avg_faithfulness": round(float(row["avg_faith"]), 4) if row.get("avg_faith") is not None else None,
        "avg_geval": round(float(row["avg_geval"]), 4) if row.get("avg_geval") is not None else None,
        "low_count": int(row.get("low") or 0),
    }


def business_quality(days: int = 7) -> dict:
    """近 N 天在线业务三率（纯 SQL 确定性口径，零 LLM 成本）：

    - dislike_rate = 点踩数 / 助手回答数；
    - followup_rate = 助手回答后 FOLLOWUP_WINDOW_SECONDS 内同会话出现用户消息的占比；
    - empty_answer_rate = 助手消息 content 为空或关联 chat trace status=error 的占比
      （关联口径：同会话中 started_at 不晚于该消息的最近一条 chat trace）。
    无样本时三率为 None（前端展示「—」，告警规则按样本门槛跳过）。
    """
    from datetime import timedelta
    since = (datetime.now() - timedelta(days=max(1, days) - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat()
    window = max(1, int(getattr(settings, "FOLLOWUP_WINDOW_SECONDS", 300)))
    row = db.query_one(
        "SELECT COUNT(*) AS n,"
        " COALESCE(SUM(CASE WHEN m.content = '' OR ("
        "   SELECT t.status FROM agent_traces t"
        "   WHERE t.agent = 'chat' AND t.session_id = m.session_id"
        "   AND t.started_at <= m.created_at"
        "   ORDER BY t.started_at DESC LIMIT 1) = 'error' THEN 1 ELSE 0 END), 0) AS empty_n,"
        " COALESCE(SUM(CASE WHEN EXISTS ("
        "   SELECT 1 FROM chat_messages u"
        "   WHERE u.session_id = m.session_id AND u.role = 'user'"
        "   AND u.created_at > m.created_at"
        # 1e-8 天（≈0.86ms）epsilon 吸收 julianday 浮点噪声，保证恰好窗口边界计入
        "   AND julianday(u.created_at) <= julianday(m.created_at) + ? / 86400.0 + 1e-8)"
        "   THEN 1 ELSE 0 END), 0) AS followup_n"
        " FROM chat_messages m WHERE m.role = 'assistant' AND m.created_at >= ?",
        (window, since)) or {}
    dislikes = int((db.query_one(
        "SELECT COUNT(*) AS n FROM message_feedbacks"
        " WHERE rating = 'dislike' AND created_at >= ?", (since,)) or {}).get("n") or 0)
    n = int(row.get("n") or 0)
    empty_n = int(row.get("empty_n") or 0)
    followup_n = int(row.get("followup_n") or 0)

    def _rate(x: int):
        return round(x / n, 4) if n else None

    return {
        "samples": n,
        "dislikes": dislikes,
        "dislike_rate": _rate(dislikes),
        "followups": followup_n,
        "followup_rate": _rate(followup_n),
        "empty_answers": empty_n,
        "empty_answer_rate": _rate(empty_n),
        "followup_window_seconds": window,
    }


def list_results(limit: int = 50, offset: int = 0) -> dict:
    """在线评测明细（API 返回前对 question/answer/reason 脱敏，原始落库不改写）。"""
    from app.core.log_setup import redact_log_text
    total = int((db.query_one("SELECT COUNT(*) AS n FROM online_eval_results") or {}).get("n") or 0)
    rows = db.query(
        "SELECT * FROM online_eval_results ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (max(1, min(200, limit)), max(0, offset)))
    items = []
    for r in rows:
        items.append({
            "id": r["id"],
            "created_at": (r.get("created_at") or "")[:16],
            "session_id": r.get("session_id") or "",
            "question": redact_log_text((r.get("question") or "").replace("\n", " ")[:120]),
            "answer": redact_log_text((r.get("answer") or "").replace("\n", " ")[:200]),
            "context_chars": int(r.get("context_chars") or 0),
            "faithfulness": r.get("faithfulness"),
            "geval_score": r.get("geval_score"),
            "geval_reason": redact_log_text(r.get("geval_reason") or ""),
            "status": r.get("status") or "ok",
        })
    return {"items": items, "total": total}


# ---------------------------------------------------------------------------
# 周期任务（lifespan 启停；测试环境不启动，显式调用 run_round）
# ---------------------------------------------------------------------------
async def _periodic_loop():
    interval = max(30, int(settings.ONLINE_EVAL_INTERVAL_SECONDS))
    while True:
        await asyncio.sleep(interval)
        try:
            await run_round()
        except Exception as e:
            logger.warning("[在线评测] 周期轮异常（下轮重试）: %s", e)


def start_periodic() -> None:
    global _PERIODIC_TASK
    if not online_eval_enabled():
        return
    if _PERIODIC_TASK and not _PERIODIC_TASK.done():
        return
    try:
        _PERIODIC_TASK = asyncio.create_task(_periodic_loop())
        logger.info("[在线评测] 周期任务已启动（间隔 %ds，采样率 %.2f）",
                    settings.ONLINE_EVAL_INTERVAL_SECONDS, settings.ONLINE_EVAL_SAMPLE_RATE)
    except RuntimeError:
        logger.warning("[在线评测] 无运行中的事件循环，周期任务未启动")


async def stop_periodic() -> None:
    global _PERIODIC_TASK
    if _PERIODIC_TASK and not _PERIODIC_TASK.done():
        _PERIODIC_TASK.cancel()
        try:
            await _PERIODIC_TASK
        except (asyncio.CancelledError, Exception):
            pass
    _PERIODIC_TASK = None