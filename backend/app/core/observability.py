"""
见字如面 - Agent 可观测性（在线监控 Agent 行为）

三个层次：
1. 日志（Logging）：专用 logger `jianziruyang.agent` 记录每一步——用户输入、LLM 调用、
   工具调用、返回结果，落入既有按日滚动日志文件（backend/log/），是最基本的可观测性。
2. 指标（Metrics）：由 agent_traces / agent_spans 聚合统计 Token 消耗、响应延迟、
   成功率、工具调用次数，供监控与告警（GET /api/observability/metrics）。
3. 追踪（Tracing）：一次用户请求 = 一条 trace；其中每一步（LLM 调用 / 工具调用 /
   流程步骤）= 一条 span。llm span 保存完整 prompt 与 response，支持全链路追踪回放。

Token 统计优先使用 LLM 层上报的真实 usage（见 app.core.llm 调用 note_usage），
未上报时（如 OpenAI 兼容流式接口）按字符数估算兜底。

trace 上下文基于 contextvars 在同一请求任务内传递；所有写库失败均静默忽略，
可观测性本身绝不能影响主流程。
"""
import contextvars
import json
import logging
import math
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

from app.core import db

# 日志层：专用 logger，与业务日志分离便于按名过滤（logging 配置已按日滚动落盘）
logger = logging.getLogger("jianziruyang.agent")

# 单个 span 的 input/output 上限：完整 prompt/response 入库但防止超长撑爆 SQLite
_MAX_SPAN_TEXT = 20000
# trace 保留天数（启动时清理过期数据）
RETENTION_DAYS = 30

# ---- 请求级上下文（同一 asyncio 任务内传递；to_thread 会复制上下文） ----
_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("agent_trace_id", default="")
_trace_started: contextvars.ContextVar[float] = contextvars.ContextVar("agent_trace_started", default=0.0)
_span_seq: contextvars.ContextVar[int] = contextvars.ContextVar("agent_span_seq", default=0)
_trace_failed: contextvars.ContextVar[bool] = contextvars.ContextVar("agent_trace_failed", default=False)
_trace_error: contextvars.ContextVar[str] = contextvars.ContextVar("agent_trace_error", default="")
# LLM 层上报的真实 usage，由下一次 record_llm 消费
_pending_usage: contextvars.ContextVar[Optional[tuple]] = contextvars.ContextVar("agent_pending_usage", default=None)


def _now() -> str:
    return datetime.now().isoformat()


def _clip(text: str, limit: int = _MAX_SPAN_TEXT) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit] + f"\n...(超出 {limit} 字符已截断)"


def estimate_tokens(text: str) -> int:
    """按字符估算 token：CJK 字符 1:1，其余字符约 4 字符/token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or "\uff00" <= ch <= "\uffef")
    return cjk + math.ceil((len(text) - cjk) / 4)


def note_usage(prompt_tokens: int, completion_tokens: int) -> None:
    """LLM 层上报真实 token usage（供下一次 record_llm 消费，优先于估算）。"""
    try:
        pt, ct = int(prompt_tokens or 0), int(completion_tokens or 0)
        if pt or ct:
            _pending_usage.set((pt, ct))
    except (TypeError, ValueError):
        pass


def mark_failed(error: str = "") -> None:
    """业务流内标记本次 trace 失败（如向前端 yield 了 error 事件但未抛异常）。"""
    if not _trace_id.get():
        return
    _trace_failed.set(True)
    if error:
        _trace_error.set(str(error)[:500])


# ---------------------------------------------------------------------------
# Tracing：trace / span 生命周期
# ---------------------------------------------------------------------------

def start_trace(agent: str, user_id: str = "", input_text: str = "") -> str:
    """开启一条 trace（一次用户请求 / 一次 Agent 运行），返回 trace_id。"""
    tid = str(uuid.uuid4())
    _trace_id.set(tid)
    _trace_started.set(time.monotonic())
    _span_seq.set(0)
    _trace_failed.set(False)
    _trace_error.set("")
    _pending_usage.set(None)
    try:
        db.execute(
            "INSERT INTO agent_traces (id, agent, user_id, input, status, started_at)"
            " VALUES (?,?,?,?, 'running', ?)",
            (tid, agent or "", user_id or "", _clip(input_text or "", 2000), _now()),
        )
    except Exception as e:  # 可观测性不阻断主流程
        logger.warning("trace 落库失败: %s", e)
    # 日志层：记录用户输入
    logger.info(
        "[AGENT] trace=%s agent=%s user=%s | 用户输入: %s",
        tid, agent or "-", user_id or "-", (input_text or "").replace("\n", " ")[:500],
    )
    return tid


def end_trace(status: str = "", error: str = "") -> None:
    """结束当前 trace：聚合 span 统计回写主表，并输出结束日志。"""
    tid = _trace_id.get()
    if not tid:
        return
    if not status:
        status = "error" if _trace_failed.get() else "ok"
    error = (error or _trace_error.get() or "")[:500]
    latency = int((time.monotonic() - _trace_started.get()) * 1000) if _trace_started.get() else 0
    agg = {"n": 0, "llm": 0, "tool": 0, "pt": 0, "ct": 0}
    try:
        row = db.query_one(
            "SELECT COUNT(*) AS n,"
            " COALESCE(SUM(kind = 'llm'), 0) AS llm,"
            " COALESCE(SUM(kind = 'tool'), 0) AS tool,"
            " COALESCE(SUM(prompt_tokens), 0) AS pt,"
            " COALESCE(SUM(completion_tokens), 0) AS ct"
            " FROM agent_spans WHERE trace_id = ?",
            (tid,),
        )
        if row:
            agg = {"n": row["n"], "llm": row["llm"], "tool": row["tool"], "pt": row["pt"], "ct": row["ct"]}
        db.execute(
            "UPDATE agent_traces SET status = ?, error = ?, latency_ms = ?, llm_calls = ?, tool_calls = ?,"
            " prompt_tokens = ?, completion_tokens = ?, ended_at = ? WHERE id = ?",
            (status, error, latency, agg["llm"], agg["tool"], agg["pt"], agg["ct"], _now(), tid),
        )
    except Exception as e:
        logger.warning("trace 结束落库失败: %s", e)
    # 日志层：请求级汇总（成功率/延迟监控的数据源之一）
    logger.info(
        "[AGENT] trace=%s end status=%s latency=%dms llm=%d tool=%d tokens=%d+%d%s",
        tid, status, latency, agg["llm"], agg["tool"], agg["pt"], agg["ct"],
        f" error={error}" if error else "",
    )
    if status == "error":
        logger.warning("[AGENT] trace=%s 失败: %s", tid, error or "未知错误")
    _trace_id.set("")


def current_trace_id() -> str:
    return _trace_id.get()


def _next_seq() -> int:
    n = _span_seq.get() + 1
    _span_seq.set(n)
    return n


def _latency(t0: Optional[float]) -> int:
    return int((time.monotonic() - t0) * 1000) if t0 else 0


def _insert_span(kind: str, name: str, input_text: str, output_text: str,
                 status: str, error: str, pt: int, ct: int, latency: int) -> None:
    tid = _trace_id.get()
    if not tid:
        return
    try:
        db.execute(
            "INSERT INTO agent_spans (id, trace_id, seq, kind, name, input, output, status, error,"
            " prompt_tokens, completion_tokens, latency_ms, started_at, ended_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()), tid, _next_seq(), kind, name or "",
                _clip(input_text), _clip(output_text), status, (error or "")[:500],
                pt, ct, latency, _now(), _now(),
            ),
        )
    except Exception as e:
        logger.warning("span 落库失败: %s", e)


def record_llm(name: str, prompt, response: str = "", t0: Optional[float] = None,
               status: str = "ok", error: str = "", fail_trace: bool = True) -> None:
    """记录一次 LLM 调用 span（追踪层存完整 prompt/response；日志层记摘要）。

    fail_trace=False 用于「已降级兜底」的失败（如流式失败转非流式），不计入 trace 失败。
    """
    prompt_text = prompt if isinstance(prompt, str) else json.dumps(prompt, ensure_ascii=False)
    usage = _pending_usage.get()
    _pending_usage.set(None)
    if usage:
        pt, ct = usage
    else:
        pt, ct = estimate_tokens(prompt_text), estimate_tokens(response or "")
    latency = _latency(t0)
    if status != "ok" and fail_trace:
        mark_failed(error)
    _insert_span("llm", name, prompt_text, response or "", status, error, pt, ct, latency)
    logger.info(
        "[AGENT] trace=%s llm=%s status=%s latency=%dms tokens=%d+%d | response: %s",
        _trace_id.get() or "-", name or "-", status, latency, pt, ct,
        (response or "").replace("\n", " ")[:200],
    )


def record_tool(name: str, args, result, t0: Optional[float] = None,
                status: str = "ok", error: str = "", fail_trace: bool = True) -> None:
    """记录一次工具调用 span（输入参数 + 返回结果）。"""
    args_text = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
    result_text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    latency = _latency(t0)
    if status != "ok" and fail_trace:
        mark_failed(error)
    _insert_span("tool", name, args_text, result_text, status, error, 0, 0, latency)
    logger.info(
        "[AGENT] trace=%s tool=%s status=%s latency=%dms | args=%s | result: %s",
        _trace_id.get() or "-", name or "-", status, latency, args_text[:200],
        result_text.replace("\n", " ")[:200],
    )


def record_step(name: str, detail: str = "", t0: Optional[float] = None,
                status: str = "ok", error: str = "", fail_trace: bool = True) -> None:
    """记录一个流程步骤 span（检索/重排/证据校验等非 LLM、非工具环节）。"""
    latency = _latency(t0)
    if status != "ok" and fail_trace:
        mark_failed(error)
    _insert_span("step", name, detail or "", "", status, error, 0, 0, latency)
    logger.info(
        "[AGENT] trace=%s step=%s status=%s latency=%dms %s",
        _trace_id.get() or "-", name or "-", status, latency, (detail or "").replace("\n", " ")[:200],
    )


# ---------------------------------------------------------------------------
# Metrics：聚合统计（监控与告警数据源）
# ---------------------------------------------------------------------------

def _since(days: int) -> str:
    return (datetime.now() - timedelta(days=max(1, days) - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()


def metrics(days: int = 7) -> dict:
    """聚合指标：总览 + 每日趋势 + 按 Agent + 按工具（Token/延迟/成功率/调用次数）。"""
    since = _since(days)
    overview = db.query_one(
        "SELECT COUNT(*) AS total,"
        " COALESCE(SUM(status = 'ok'), 0) AS ok,"
        " COALESCE(SUM(status = 'error'), 0) AS errors,"
        " COALESCE(AVG(latency_ms), 0) AS avg_latency,"
        " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
        " COALESCE(SUM(llm_calls), 0) AS llm_calls,"
        " COALESCE(SUM(tool_calls), 0) AS tool_calls"
        " FROM agent_traces WHERE started_at >= ? AND status != 'running'",
        (since,),
    ) or {}
    finished = int(overview.get("ok", 0)) + int(overview.get("errors", 0))
    latencies = [
        r["latency_ms"] for r in db.query(
            "SELECT latency_ms FROM agent_traces"
            " WHERE started_at >= ? AND status != 'running' ORDER BY started_at DESC LIMIT 5000",
            (since,),
        )
    ]
    p95 = 0
    if latencies:
        latencies.sort()
        p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
    daily = [
        dict(r) for r in db.query(
            "SELECT date(started_at) AS day, COUNT(*) AS total,"
            " COALESCE(SUM(status = 'ok'), 0) AS ok,"
            " COALESCE(SUM(status = 'error'), 0) AS errors,"
            " COALESCE(AVG(latency_ms), 0) AS avg_latency,"
            " COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens"
            " FROM agent_traces WHERE started_at >= ? AND status != 'running'"
            " GROUP BY day ORDER BY day",
            (since,),
        )
    ]
    by_agent = [
        dict(r) for r in db.query(
            "SELECT agent, COUNT(*) AS total,"
            " COALESCE(SUM(status = 'ok'), 0) AS ok,"
            " COALESCE(SUM(status = 'error'), 0) AS errors,"
            " COALESCE(AVG(latency_ms), 0) AS avg_latency,"
            " COALESCE(SUM(llm_calls), 0) AS llm_calls,"
            " COALESCE(SUM(tool_calls), 0) AS tool_calls,"
            " COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens"
            " FROM agent_traces WHERE started_at >= ? AND status != 'running'"
            " GROUP BY agent ORDER BY total DESC",
            (since,),
        )
    ]
    by_tool = [
        dict(r) for r in db.query(
            "SELECT name AS tool, COUNT(*) AS total,"
            " COALESCE(SUM(status = 'ok'), 0) AS ok,"
            " COALESCE(SUM(status = 'error'), 0) AS errors,"
            " COALESCE(AVG(latency_ms), 0) AS avg_latency"
            " FROM agent_spans WHERE kind = 'tool' AND started_at >= ?"
            " GROUP BY name ORDER BY total DESC",
            (since,),
        )
    ]
    return {
        "days": days,
        "overview": {
            "total": int(overview.get("total", 0)),
            "ok": int(overview.get("ok", 0)),
            "errors": int(overview.get("errors", 0)),
            "success_rate": round(int(overview.get("ok", 0)) / finished, 4) if finished else 0.0,
            "avg_latency_ms": int(overview.get("avg_latency", 0)),
            "p95_latency_ms": int(p95),
            "prompt_tokens": int(overview.get("prompt_tokens", 0)),
            "completion_tokens": int(overview.get("completion_tokens", 0)),
            "llm_calls": int(overview.get("llm_calls", 0)),
            "tool_calls": int(overview.get("tool_calls", 0)),
        },
        "daily": daily,
        "by_agent": by_agent,
        "by_tool": by_tool,
    }


# ---------------------------------------------------------------------------
# Tracing 查询：trace 列表 / 详情（含完整 prompt/response）
# ---------------------------------------------------------------------------

def list_traces(agent: str = "", status: str = "", limit: int = 30, offset: int = 0) -> dict:
    sql = "SELECT * FROM agent_traces WHERE 1=1"
    count_sql = "SELECT COUNT(*) AS n FROM agent_traces WHERE 1=1"
    params: list = []
    if agent:
        sql += " AND agent = ?"
        count_sql += " AND agent = ?"
        params.append(agent)
    if status:
        sql += " AND status = ?"
        count_sql += " AND status = ?"
        params.append(status)
    total = db.query_one(count_sql, tuple(params))
    sql += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
    rows = db.query(sql, tuple(params + [limit, offset]))
    return {"total": total["n"] if total else 0, "items": [dict(r) for r in rows]}


def get_trace(trace_id: str) -> Optional[dict]:
    row = db.query_one("SELECT * FROM agent_traces WHERE id = ?", (trace_id,))
    if not row:
        return None
    spans = [dict(r) for r in db.query(
        "SELECT * FROM agent_spans WHERE trace_id = ? ORDER BY seq", (trace_id,)
    )]
    return {**dict(row), "spans": spans}


def purge_old(days: int = RETENTION_DAYS) -> int:
    """清理过期 trace/span（启动时调用，防止完整 prompt 长期撑大数据库）。"""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    try:
        db.execute("DELETE FROM agent_spans WHERE trace_id IN"
                   " (SELECT id FROM agent_traces WHERE started_at < ?)", (cutoff,))
        n = db.execute("DELETE FROM agent_traces WHERE started_at < ?", (cutoff,))
        if n:
            logger.info("已清理 %d 条过期 Agent trace（保留 %d 天）", n, days)
        return n
    except Exception as e:
        logger.warning("清理过期 trace 失败: %s", e)
        return 0
