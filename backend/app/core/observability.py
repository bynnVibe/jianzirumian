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
from app.config import settings

# 日志层：专用 logger，与业务日志分离便于按名过滤（logging 配置已按日滚动落盘）
logger = logging.getLogger("jianziruyang.agent")

# 单个 span 的 input/output 上限：完整 prompt/response 入库但防止超长撑爆 SQLite
_MAX_SPAN_TEXT = 20000
# trace 保留天数（启动时清理过期数据）
RETENTION_DAYS = 30

# segment 级 token 度量的中文展示名（供管理员可视化按段定位 Token 大户）
SEGMENT_LABELS = {
    "system": "系统提示",
    "memory": "长期记忆",
    "memory_profile": "记忆·画像卡",
    "memory_relevant": "记忆·相关检索",
    "skills": "用户 Skill",
    "history": "历史对话",
    "summary": "历史摘要",
    "knowledge": "知识上下文",
    "search": "联网搜索",
    "tool": "附件工具",
}

# ---- 请求级上下文（同一 asyncio 任务内传递；to_thread 会复制上下文） ----
_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("agent_trace_id", default="")
_trace_agent: contextvars.ContextVar[str] = contextvars.ContextVar("agent_trace_agent", default="")
_trace_started: contextvars.ContextVar[float] = contextvars.ContextVar("agent_trace_started", default=0.0)
_span_seq: contextvars.ContextVar[int] = contextvars.ContextVar("agent_span_seq", default=0)
_trace_failed: contextvars.ContextVar[bool] = contextvars.ContextVar("agent_trace_failed", default=False)
_trace_error: contextvars.ContextVar[str] = contextvars.ContextVar("agent_trace_error", default="")
# LLM 层上报的真实 usage，由下一次 record_llm 消费
_pending_usage: contextvars.ContextVar[Optional[tuple]] = contextvars.ContextVar("agent_pending_usage", default=None)
# 本次请求累积的上下文各段 (segment, text, 估算token)，由 flush_segments 按真实口径落库
_pending_segments: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar("agent_pending_segments", default=None)


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

def start_trace(agent: str, user_id: str = "", input_text: str = "", session_id: str = "") -> str:
    """开启一条 trace（一次用户请求 / 一次 Agent 运行），返回 trace_id。

    session_id 可选：聊天主链传入会话 ID，使 trace（及其 segment 构成）可按对话窗口聚合。
    """
    tid = str(uuid.uuid4())
    _trace_id.set(tid)
    _trace_agent.set(agent or "")
    _trace_started.set(time.monotonic())
    _span_seq.set(0)
    _trace_failed.set(False)
    _trace_error.set("")
    _pending_usage.set(None)
    _pending_segments.set(None)
    try:
        db.execute(
            "INSERT INTO agent_traces (id, agent, user_id, input, status, started_at, session_id)"
            " VALUES (?,?,?,?, 'running', ?,?)",
            (tid, agent or "", user_id or "", _clip(input_text or "", 2000), _now(), session_id or ""),
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
            " prompt_tokens = ?, completion_tokens = ?, ended_at = ? WHERE id = ? AND status = 'running'",
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
               status: str = "ok", error: str = "", fail_trace: bool = True) -> tuple:
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
    return pt, ct


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


# segment 级 Token 度量：按语义段沉淀上下文构成，供管理员定位 Token 大户与量化优化效果。
# 标准段名（前端展示中文映射）：system / memory / history / knowledge / search / tool。
# 口径约定：各段 token 与成本口径（LLM 真实 usage）同源——以最终生成调用的真实 prompt_tokens
# 为总量、按各段估算占比分摊，保证构成口径与成本口径度量维度一致、可直接对比分析。
def note_segment(segment: str, text: str) -> int:
    """累积一次请求内某个上下文语义段的原始文本（暂不落库），返回估算 token 供调用方参考。

    真实 token 口径的落库由 flush_segments 在拿到最终生成调用的真实 usage 后统一完成。
    """
    est = estimate_tokens(text or "")
    segs = _pending_segments.get()
    if segs is None:
        segs = []
        _pending_segments.set(segs)
    segs.append((segment or "", text or "", est))
    return est


def flush_segments(prompt, real_prompt_tokens: Optional[int] = None) -> None:
    """把本请求累积的上下文各段按真实 token 口径落库（沉淀到 agent_token_segments）。

    以最终生成调用（answer）的真实 prompt_tokens 为总量，按各段估算占比分摊到各段，
    使各段 token 与成本口径处于同一度量维度；无真实 usage（估算兜底）时 scale=1。
    无活动 trace 或无累积段时静默跳过（可观测性不阻断主流程）。
    """
    segs = _pending_segments.get()
    _pending_segments.set(None)
    tid = _trace_id.get()
    if not segs or not tid:
        return
    prompt_text = prompt if isinstance(prompt, str) else json.dumps(prompt, ensure_ascii=False)
    est_full = estimate_tokens(prompt_text)
    scale = (int(real_prompt_tokens) / est_full) if (est_full and real_prompt_tokens) else 1.0
    for segment, text, est in segs:
        n = int(round(est * scale))
        try:
            db.execute(
                "INSERT INTO agent_token_segments (id, trace_id, agent, segment, tokens, chars, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), tid, _trace_agent.get() or "", segment, n, len(text), _now()),
            )
        except Exception as e:  # 可观测性不阻断主流程
            logger.warning("segment token 落库失败: %s", e)
        logger.info("[AGENT] trace=%s segment=%s tokens=%d chars=%d scale=%.3f",
                    tid, segment or "-", n, len(text), scale)


# ---------------------------------------------------------------------------
# Metrics：聚合统计（监控与告警数据源）
# ---------------------------------------------------------------------------

def _since(days: int) -> str:
    return (datetime.now() - timedelta(days=max(1, days) - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()


def personal_metrics(user_id: str, days: int = 7) -> dict:
    """仅聚合当前账号的业务指标；不读取正文、步骤、模型或工具数据。"""
    if not user_id or user_id in {"guest", "system", "-"}:
        raise ValueError("个人统计需要有效的登录用户")
    days = max(1, min(days, RETENTION_DAYS))
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=days - 1)
    end = today + timedelta(days=1)
    rows = db.query(
        "SELECT substr(started_at, 1, 10) AS day,"
        " CASE agent WHEN 'chat' THEN '智能问答'"
        " WHEN 'wiki_assistant' THEN '知识助手'"
        " WHEN 'wiki_compile' THEN '笔记整理'"
        " WHEN 'wiki_synthesize' THEN '综合报告'"
        " WHEN 'chat_confirmation' THEN '资料入库'"
        " WHEN 'urlAnalystAgent' THEN '链接解析'"
        " WHEN 'url_import_confirmation' THEN '资料入库'"
        " ELSE '其他服务' END AS activity,"
        " COUNT(*) AS total,"
        " SUM(status = 'ok') AS succeeded,"
        " SUM(status NOT IN ('ok', 'running', 'cancelled')) AS failed,"
        " SUM(status = 'cancelled') AS cancelled,"
        " SUM(status = 'running') AS processing,"
        " SUM(CASE WHEN status NOT IN ('running', 'cancelled')"
        " THEN MAX(COALESCE(latency_ms, 0), 0) ELSE 0 END) AS elapsed_ms"
        " FROM agent_traces WHERE user_id = ? AND started_at >= ? AND started_at < ?"
        " GROUP BY day, activity ORDER BY day, activity",
        (user_id, start.isoformat(), end.isoformat()),
    )
    fields = ("total", "succeeded", "failed", "cancelled", "processing", "elapsed_ms")

    def empty():
        return dict.fromkeys(fields, 0)

    def business_values(counts):
        completed = counts["succeeded"] + counts["failed"]
        return {
            "total": counts["total"],
            "completed": completed,
            "succeeded": counts["succeeded"],
            "failed": counts["failed"],
            "cancelled": counts["cancelled"],
            "processing": counts["processing"],
            "completion_rate": round(counts["succeeded"] / completed, 4) if completed else None,
            "avg_response_seconds": round(counts["elapsed_ms"] / completed / 1000, 1) if completed else None,
        }

    overview = empty()
    daily = {(start + timedelta(days=i)).date().isoformat(): empty() for i in range(days)}
    activities = {}
    for row in rows:
        activity = activities.setdefault(row["activity"], empty())
        for bucket in (overview, daily[row["day"]], activity):
            for field in fields:
                bucket[field] += int(row.get(field) or 0)
    return {
        "days": days,
        "retention_days": RETENTION_DAYS,
        "overview": business_values(overview),
        "daily": [{"day": day, **business_values(counts)} for day, counts in daily.items()],
        "by_activity": [
            {"activity": name, **business_values(counts)}
            for name, counts in sorted(activities.items(), key=lambda item: -item[1]["total"])
        ],
    }


def personal_context_overview(user_id: str, days: int = 7) -> dict:
    """当前用户的「上下文窗口 + 记忆模块」使用概览（可观测性个人面板数据源）。

    - 上下文窗口：按语义段（系统/记忆/历史/知识/搜索/工具…）聚合该用户近期每次请求
      注入上下文的 token 构成，并给出单请求 prompt token 的均值/峰值与告警水位；
    - 历史压缩：该用户名下已生成会话摘要的数量与摘要 token 占用（compaction 概览）；
    - 记忆模块：委托 MemoryService.usage_overview（条数/分类型/画像卡/检索命中/字符与预算）。
    仅读取当前账号数据；无记录时返回结构完整的空概览。
    """
    if not user_id or user_id in {"guest", "system", "-"}:
        raise ValueError("个人上下文概览需要有效的登录用户")
    days = max(1, min(days, RETENTION_DAYS))
    since = _since(days)

    # 上下文窗口：该用户单请求真实 prompt token（= 注入上下文总量）的分布
    win = db.query_one(
        "SELECT COUNT(*) AS n,"
        " COALESCE(AVG(prompt_tokens), 0) AS avg_pt,"
        " COALESCE(MAX(prompt_tokens), 0) AS peak_pt,"
        " COALESCE(SUM(prompt_tokens), 0) AS sum_pt"
        " FROM agent_traces WHERE user_id = ? AND started_at >= ? AND status != 'running'",
        (user_id, since),
    ) or {}
    warn_threshold = int(getattr(settings, "TOKEN_AVG_WARN_PER_REQUEST", 6000))
    avg_pt = round(float(win.get("avg_pt", 0) or 0), 1)
    requests = int(win.get("n", 0) or 0)

    # segment 级构成：该用户各语义段占用（定位上下文窗口里哪段最大）
    seg_rows = db.query(
        "SELECT s.segment, COALESCE(SUM(s.tokens), 0) AS tokens,"
        " COALESCE(SUM(s.chars), 0) AS chars, COUNT(*) AS n,"
        " COALESCE(MAX(s.tokens), 0) AS peak"
        " FROM agent_token_segments s JOIN agent_traces t ON t.id = s.trace_id"
        " WHERE t.user_id = ? AND s.created_at >= ?"
        " GROUP BY s.segment ORDER BY tokens DESC",
        (user_id, since),
    )
    seg_total = sum(int(r["tokens"]) for r in seg_rows)
    segments = [
        {
            "segment": r["segment"],
            "label": SEGMENT_LABELS.get(r["segment"], r["segment"]),
            "tokens": int(r["tokens"]),
            "chars": int(r["chars"]),
            "count": int(r["n"]),
            "avg_tokens": round(int(r["tokens"]) / int(r["n"]), 1) if int(r["n"]) else 0.0,
            "peak_tokens": int(r["peak"]),
            "share": round(int(r["tokens"]) / seg_total, 4) if seg_total else 0.0,
        }
        for r in seg_rows
    ]

    # 历史压缩（compaction）：该用户名下已生成摘要的会话数量与摘要 token 占用
    comp = db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(token_estimate), 0) AS tokens"
        " FROM chat_session_summaries WHERE user_id = ?",
        (user_id,),
    ) or {}

    from app.services.memory import MemoryService

    return {
        "days": days,
        "context_window": {
            "requests": requests,
            "avg_prompt_tokens": avg_pt,
            "peak_prompt_tokens": int(win.get("peak_pt", 0) or 0),
            "total_prompt_tokens": int(win.get("sum_pt", 0) or 0),
            "warn_threshold": warn_threshold,
            "overuse": bool(requests and avg_pt > warn_threshold),
            "history_rounds": int(getattr(settings, "SESSION_MAX_HISTORY", 8)),
            "memory_ctx_chars_budget": int(getattr(settings, "MEMORY_CTX_CHARS", 600)),
            "segments": segments,
        },
        "compression": {
            "sessions_with_summary": int(comp.get("n", 0) or 0),
            "summary_tokens": int(comp.get("tokens", 0) or 0),
            "trigger_tokens": int(getattr(settings, "CONTEXT_COMPACT_TRIGGER_TOKENS", 6000)),
        },
        "memory": MemoryService.usage_overview(user_id),
    }


def session_context_usage(session_id: str) -> dict:
    """单个对话窗口的上下文用量：该会话最近一次已完成 chat 请求的各语义段 token 构成。

    数据源与管理员/个人面板的上下文构成同口径（agent_token_segments，按最终生成调用
    真实 prompt_tokens 分摊），但按会话取最近一条 trace，供聊天页「上下文用量」缩略图
    与明细弹层展示；总量取 trace 真实 prompt_tokens（缺失时退化为段合计）。
    无记录（新会话/未发起问答）时返回结构完整的空概览。
    """
    window = int(getattr(settings, "CHAT_CONTEXT_WINDOW", 131072))
    empty = {
        "has_data": False, "segments": [], "total_tokens": 0,
        "context_window": window, "used_pct": 0.0, "trace_at": "",
    }
    if not session_id:
        return empty
    trace = db.query_one(
        "SELECT id, prompt_tokens, started_at FROM agent_traces"
        " WHERE session_id = ? AND agent = 'chat' AND status != 'running'"
        " ORDER BY started_at DESC LIMIT 1",
        (session_id,),
    )
    if not trace:
        return empty
    rows = db.query(
        "SELECT segment, COALESCE(SUM(tokens), 0) AS tokens"
        " FROM agent_token_segments WHERE trace_id = ?"
        " GROUP BY segment ORDER BY tokens DESC",
        (trace["id"],),
    )
    seg_total = sum(int(r["tokens"]) for r in rows)
    total = int(trace["prompt_tokens"] or 0) or seg_total
    segments = [
        {
            "segment": r["segment"],
            "label": SEGMENT_LABELS.get(r["segment"], r["segment"]),
            "tokens": int(r["tokens"]),
            "share": round(int(r["tokens"]) / seg_total, 4) if seg_total else 0.0,
        }
        for r in rows
    ]
    return {
        "has_data": True,
        "segments": segments,
        "total_tokens": total,
        "context_window": window,
        "used_pct": round(total / window * 100, 1) if window else 0.0,
        "trace_at": trace["started_at"],
    }


def personal_session_contexts(user_id: str, days: int = 7, limit: int = 20) -> dict:
    """按对话窗口下钻的上下文构成（个人面板总集口径的会话级子集切片）。

    个人面板的「上下文构成」是周期内该用户全部请求的按段累计（总集）；本函数按
    session_id 切片为逐对话窗口的子集：每会话的请求数/均值/峰值/最近活跃时间，
    及该会话最近一次已完成请求的 segment 构成（与聊天页缩略图同口径）。
    未关联会话的历史存量 trace（session_id 为空）不计入。
    """
    if not user_id or user_id in {"guest", "system", "-"}:
        raise ValueError("按对话下钻上下文需要有效的登录用户")
    days = max(1, min(days, RETENTION_DAYS))
    limit = max(1, min(limit, 50))
    since = _since(days)
    rows = db.query(
        "SELECT t.session_id, COUNT(*) AS n,"
        " COALESCE(AVG(t.prompt_tokens), 0) AS avg_pt,"
        " COALESCE(MAX(t.prompt_tokens), 0) AS peak_pt,"
        " COALESCE(SUM(t.prompt_tokens), 0) AS sum_pt,"
        " MAX(t.started_at) AS last_at"
        " FROM agent_traces t"
        " WHERE t.user_id = ? AND t.started_at >= ? AND t.status != 'running'"
        " AND t.session_id != ''"
        " GROUP BY t.session_id ORDER BY last_at DESC LIMIT ?",
        (user_id, since, limit),
    )
    items = []
    for r in rows:
        sid = r["session_id"]
        meta = db.query_one(
            "SELECT title FROM chat_sessions WHERE session_id = ?", (sid,),
        )
        trace = db.query_one(
            "SELECT id FROM agent_traces"
            " WHERE session_id = ? AND status != 'running'"
            " ORDER BY started_at DESC LIMIT 1",
            (sid,),
        )
        seg_rows = db.query(
            "SELECT segment, COALESCE(SUM(tokens), 0) AS tokens"
            " FROM agent_token_segments WHERE trace_id = ?"
            " GROUP BY segment ORDER BY tokens DESC",
            (trace["id"],),
        ) if trace else []
        seg_total = sum(int(x["tokens"]) for x in seg_rows)
        items.append({
            "session_id": sid,
            "title": (meta or {}).get("title") or "新对话",
            "requests": int(r["n"]),
            "avg_prompt_tokens": round(float(r["avg_pt"] or 0), 1),
            "peak_prompt_tokens": int(r["peak_pt"] or 0),
            "total_prompt_tokens": int(r["sum_pt"] or 0),
            "last_at": r["last_at"],
            "latest_segments": [
                {
                    "segment": x["segment"],
                    "label": SEGMENT_LABELS.get(x["segment"], x["segment"]),
                    "tokens": int(x["tokens"]),
                    "share": round(int(x["tokens"]) / seg_total, 4) if seg_total else 0.0,
                }
                for x in seg_rows
            ],
        })
    return {"days": days, "sessions": items}


def metrics(days: int = 7) -> dict:
    """聚合指标：总览 + 每日趋势 + 按 Agent + 按工具（Token/延迟/成功率/调用次数）。"""
    since = _since(days)
    overview = db.query_one(
        "SELECT COUNT(*) AS total,"
        " COALESCE(SUM(status = 'ok'), 0) AS ok,"
        " COALESCE(SUM(status = 'error'), 0) AS errors,"
                " COALESCE(SUM(status = 'cancelled'), 0) AS cancelled,"
                " COALESCE(SUM(status = 'timeout'), 0) AS timeout,"
                " COALESCE(SUM(status = 'interrupted'), 0) AS interrupted,"
        " COALESCE(AVG(latency_ms), 0) AS avg_latency,"
        " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
        " COALESCE(SUM(llm_calls), 0) AS llm_calls,"
        " COALESCE(SUM(tool_calls), 0) AS tool_calls"
        " FROM agent_traces WHERE started_at >= ? AND status != 'running'",
        (since,),
    ) or {}
    finished = int(overview.get("total", 0))
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
                    " COALESCE(SUM(status = 'cancelled'), 0) AS cancelled,"
                    " COALESCE(SUM(status = 'timeout'), 0) AS timeout,"
                    " COALESCE(SUM(status = 'interrupted'), 0) AS interrupted,"
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
                    " COALESCE(SUM(status = 'cancelled'), 0) AS cancelled,"
                    " COALESCE(SUM(status = 'timeout'), 0) AS timeout,"
                    " COALESCE(SUM(status = 'interrupted'), 0) AS interrupted,"
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
                    " COALESCE(SUM(status = 'cancelled'), 0) AS cancelled,"
                    " COALESCE(SUM(status = 'timeout'), 0) AS timeout,"
                    " COALESCE(SUM(status = 'interrupted'), 0) AS interrupted,"
            " COALESCE(AVG(latency_ms), 0) AS avg_latency"
            " FROM agent_spans WHERE kind = 'tool' AND started_at >= ?"
            " GROUP BY name ORDER BY total DESC",
            (since,),
        )
    ]

    # ---- Token 用量分析（管理员可视化：是否过多 / 是否下降 / 各段构成）----
    cur_total_tokens = int(overview.get("prompt_tokens", 0)) + int(overview.get("completion_tokens", 0))
    avg_tokens = round(cur_total_tokens / finished, 1) if finished else 0.0
    warn_threshold = int(getattr(settings, "TOKEN_AVG_WARN_PER_REQUEST", 6000))
    # 每日平均 token（供趋势图判断逐日升/降）
    for r in daily:
        r["avg_tokens"] = round(int(r.get("tokens", 0)) / int(r.get("total", 0)), 1) if int(r.get("total", 0)) else 0.0
    # 对比基线：本周期内按时间对半拆分，「前期半段」正常对话平均作为基线、「近期半段」作为当前，
    # 保证只要周期内≥2 次正常对话就有升/降对比（回答“是否下降/优化”），不依赖周期外历史数据。
    trace_series = [
        int(r["tokens"]) for r in db.query(
            "SELECT (prompt_tokens + completion_tokens) AS tokens"
            " FROM agent_traces WHERE started_at >= ? AND status = 'ok' ORDER BY started_at",
            (since,),
        )
    ]
    if len(trace_series) >= 2:
        half = len(trace_series) // 2
        earlier, later = trace_series[:half], trace_series[half:]
    else:
        earlier, later = [], trace_series
    earlier_n, later_n = len(earlier), len(later)
    earlier_sum, later_sum = sum(earlier), sum(later)
    prev_avg = round(earlier_sum / earlier_n, 1) if earlier_n else 0.0
    recent_avg = round(later_sum / later_n, 1) if later_n else 0.0
    delta_pct = round((recent_avg - prev_avg) / prev_avg * 100, 1) if (earlier_n and prev_avg) else None
    # segment 级构成：哪一段占用 Token 最多（定位优化重点）
    seg_rows = db.query(
        "SELECT segment, COALESCE(SUM(tokens), 0) AS tokens, COUNT(*) AS n"
        " FROM agent_token_segments WHERE created_at >= ? GROUP BY segment ORDER BY tokens DESC",
        (since,),
    )
    seg_total = sum(int(r["tokens"]) for r in seg_rows)
    # 每段的对比基线：本周期内该段按时间对半拆分，「前期半段」平均为基线、「近期半段」为当前，
    # 保证只要该段≥2 次记录就有升/降对比（回答“该段数量上是否有优化”）。
    seg_series: dict = {}
    for r in db.query(
        "SELECT s.segment, s.tokens"
        " FROM agent_token_segments s JOIN agent_traces t ON t.id = s.trace_id"
        " WHERE s.created_at >= ? AND t.status = 'ok' ORDER BY s.created_at",
        (since,),
    ):
        seg_series.setdefault(r["segment"], []).append(int(r["tokens"]))
    # 每段对应的具体会话明细（trace_id/输入摘要/token/时间/用户），供前端 drill-down 定位“对应哪次对话”
    seg_items: dict = {}
    for r in db.query(
        "SELECT s.segment, s.trace_id, s.tokens, s.created_at, t.input, t.user_id"
        " FROM agent_token_segments s LEFT JOIN agent_traces t ON t.id = s.trace_id"
        " WHERE s.created_at >= ? ORDER BY s.created_at DESC",
        (since,),
    ):
        bucket = seg_items.setdefault(r["segment"], [])
        if len(bucket) < 10:
            bucket.append(r)
    item_user_ids = {r["user_id"] for rows in seg_items.values() for r in rows}
    uname_map = _resolve_usernames(item_user_ids)
    segment_breakdown = []
    for r in seg_rows:
        seg = r["segment"]
        n = int(r["n"])
        avg = round(int(r["tokens"]) / n, 1) if n else 0.0
        series = seg_series.get(seg, [])
        if len(series) >= 2:
            shalf = len(series) // 2
            s_earlier, s_later = series[:shalf], series[shalf:]
            p_avg = round(sum(s_earlier) / len(s_earlier), 1)
            r_avg = round(sum(s_later) / len(s_later), 1)
            delta = round((r_avg - p_avg) / p_avg * 100, 1) if p_avg else None
        else:
            p_avg = 0.0
            r_avg = avg
            delta = None
        segment_breakdown.append({
            "segment": seg,
            "label": SEGMENT_LABELS.get(seg, seg),
            "tokens": int(r["tokens"]),
            "count": n,
            "avg_tokens": avg,
            "share": round(int(r["tokens"]) / seg_total, 4) if seg_total else 0.0,
            # 该段近期/前期平均与升降（负=下降=优化见效）
            "recent_avg": r_avg,
            "prev_avg": p_avg,
            "delta_pct": delta,
            "trend": "flat" if not delta else ("up" if delta > 0 else "down"),
            # 该段对应的具体会话（最近 10 条，倒序）
            "items": [
                {
                    "trace_id": it["trace_id"],
                    "input": (it["input"] or "").replace("\n", " ")[:60],
                    "tokens": int(it["tokens"]),
                    "started_at": it["created_at"],
                    "username": uname_map.get(it["user_id"] or "", it["user_id"] or "-"),
                }
                for it in seg_items.get(seg, [])
            ],
        })
    return {
        "days": days,
        "overview": {
            "total": int(overview.get("total", 0)),
            "ok": int(overview.get("ok", 0)),
            "errors": int(overview.get("errors", 0)),
            "cancelled": int(overview.get("cancelled", 0)),
            "timeout": int(overview.get("timeout", 0)),
            "interrupted": int(overview.get("interrupted", 0)),
            "success_rate": round(int(overview.get("ok", 0)) / finished, 4) if finished else 0.0,
            "avg_latency_ms": int(overview.get("avg_latency", 0)),
            "p95_latency_ms": int(p95),
            "prompt_tokens": int(overview.get("prompt_tokens", 0)),
            "completion_tokens": int(overview.get("completion_tokens", 0)),
            "llm_calls": int(overview.get("llm_calls", 0)),
            "tool_calls": int(overview.get("tool_calls", 0)),
            # Token 用量量化指标
            "total_tokens": cur_total_tokens,
            "avg_tokens_per_request": avg_tokens,
            "token_warn_threshold": warn_threshold,
            "token_overuse": bool(finished and avg_tokens > warn_threshold),
        },
        "daily": daily,
        "by_agent": by_agent,
        "by_tool": by_tool,
        "token_comparison": {
            "days": days,
            "current_avg": recent_avg,
            "previous_avg": prev_avg,
            "current_total": later_sum,
            "previous_total": earlier_sum,
            "current_requests": later_n,
            "previous_requests": earlier_n,
            "delta_pct": delta_pct,  # 正=上升；负=下降（优化见效）；None=周期内不足 2 次
            "trend": "flat" if not delta_pct else ("up" if delta_pct > 0 else "down"),
        },
        "segment_breakdown": segment_breakdown,
    }


# ---------------------------------------------------------------------------
# Tracing 查询：trace 列表 / 详情（含完整 prompt/response）
# ---------------------------------------------------------------------------

# trace 中 user_id 为以下特殊值时不做用户名映射，前端直接原样展示
_GUEST_USER_IDS = {"", "-", "guest", "system"}


def _resolve_usernames(user_ids) -> dict:
    """按 user_id 批量查询 users 表，返回 {user_id: username} 映射。

    - 空/游客/系统等特殊 id 不参与查询（前端回退展示原值）。
    - 查询失败或用户不存在时静默忽略，可观测性绝不能影响主流程。
    """
    ids = {u for u in user_ids if u and u not in _GUEST_USER_IDS}
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    try:
        rows = db.query(
            f"SELECT id, username FROM users WHERE id IN ({placeholders})",
            tuple(ids),
        )
    except Exception:
        return {}
    mapping = {}
    for r in rows:
        row = dict(r)
        uname = row.get("username")
        if row.get("id") and uname:
            mapping[row["id"]] = uname
    return mapping


def _attach_username(item: dict, mapping: dict) -> dict:
    """为单条 trace 记录补充 username 字段（查不到则回退为 user_id）。"""
    uid = item.get("user_id") or ""
    if uid in _GUEST_USER_IDS:
        item["username"] = uid
    else:
        item["username"] = mapping.get(uid, uid)
    return item


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
    items = [dict(r) for r in rows]
    mapping = _resolve_usernames([it.get("user_id") for it in items])
    items = [_attach_username(it, mapping) for it in items]
    return {"total": total["n"] if total else 0, "items": items}


def get_trace(trace_id: str) -> Optional[dict]:
    row = db.query_one("SELECT * FROM agent_traces WHERE id = ?", (trace_id,))
    if not row:
        return None
    spans = [dict(r) for r in db.query(
        "SELECT * FROM agent_spans WHERE trace_id = ? ORDER BY seq", (trace_id,)
    )]
    item = {**dict(row), "spans": spans}
    mapping = _resolve_usernames([item.get("user_id")])
    return _attach_username(item, mapping)


def get_trace_logs(trace_id: str, cursor: str = "", limit: int = 100) -> Optional[dict]:
    """只读取指定请求的日志，不加载无关请求或将时间邻近视为关联。"""
    from app.core.log_setup import read_trace_logs

    trace = db.query_one("SELECT id, started_at, ended_at FROM agent_traces WHERE id = ?", (trace_id,))
    if not trace:
        return None
    return read_trace_logs(dict(trace), cursor, limit)


def purge_old(days: int = RETENTION_DAYS) -> int:
    """清理过期 trace/span（启动时调用，防止完整 prompt 长期撑大数据库）。"""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    try:
        db.execute("DELETE FROM agent_spans WHERE trace_id IN"
                   " (SELECT id FROM agent_traces WHERE started_at < ?)", (cutoff,))
        db.execute("DELETE FROM agent_token_segments WHERE created_at < ?", (cutoff,))
        n = db.execute("DELETE FROM agent_traces WHERE started_at < ?", (cutoff,))
        if n:
            logger.info("已清理 %d 条过期 Agent trace（保留 %d 天）", n, days)
        return n
    except Exception as e:
        logger.warning("清理过期 trace 失败: %s", e)
        return 0
