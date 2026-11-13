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


def note_usage(prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> None:
    """LLM 层上报真实 token usage（供下一次 record_llm 消费，优先于估算）。

    cached_tokens：上游命中前缀缓存的 prompt token 数（OpenAI prompt_tokens_details.cached_tokens /
    Anthropic cache_read_input_tokens），用于量化 Prompt 分区整改的缓存命中收益。
    """
    try:
        pt, ct, cached = int(prompt_tokens or 0), int(completion_tokens or 0), int(cached_tokens or 0)
        if pt or ct:
            _pending_usage.set((pt, ct, cached))
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
                 status: str, error: str, pt: int, ct: int, latency: int,
                 cached: int = 0) -> None:
    tid = _trace_id.get()
    if not tid:
        return
    try:
        db.execute(
            "INSERT INTO agent_spans (id, trace_id, seq, kind, name, input, output, status, error,"
            " prompt_tokens, completion_tokens, cached_tokens, latency_ms, started_at, ended_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()), tid, _next_seq(), kind, name or "",
                _clip(input_text), _clip(output_text), status, (error or "")[:500],
                pt, ct, cached, latency, _now(), _now(),
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
    cached = 0
    if usage:
        # 兼容旧 2-tuple 上报（无 cached_tokens）与新 3-tuple
        pt, ct = usage[0], usage[1]
        cached = usage[2] if len(usage) > 2 else 0
    else:
        pt, ct = estimate_tokens(prompt_text), estimate_tokens(response or "")
    latency = _latency(t0)
    if status != "ok" and fail_trace:
        mark_failed(error)
    _insert_span("llm", name, prompt_text, response or "", status, error, pt, ct, latency, cached)
    logger.info(
        "[AGENT] trace=%s llm=%s status=%s latency=%dms tokens=%d+%d cached=%d | response: %s",
        _trace_id.get() or "-", name or "-", status, latency, pt, ct, cached,
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


# ---------------------------------------------------------------------------
# Tool Routing 调用链分析（工具治理可观测性）
# ---------------------------------------------------------------------------
# 一次工具调用 = 一条 tool_routing 记录，由 tools.invoke.invoke_tool 在
# 「约束钩子 → 执行」全链路落库；聚合出五项核心指标（见 tool_routing_metrics）。
# 与 agent_spans(kind='tool') 互补：span 记录「调用内容/结果文本」，routing 记录
# 「治理维度」（参数合法/越权/风险/限流/Schema Token/延迟），专供 Tool Routing 面板。

def record_tool_routing(
    *,
    tool_name: str,
    namespace: str = "",
    user_id: str = "",
    session_id: str = "",
    trace_id: str = "",
    routed_by: str = "",
    args: Optional[dict] = None,
    risk_level: str = "low",
    schema_valid: bool = True,
    permission_ok: bool = True,
    risk_blocked: bool = False,
    rate_limited: bool = False,
    allowed: bool = True,
    blocked_code: str = "",
    status: str = "ok",
    success: bool = True,
    schema_tokens: int = 0,
    t0: Optional[float] = None,
    error: str = "",
) -> None:
    """落库一条 Tool Routing 记录（可观测性绝不影响主流程，失败静默忽略）。

    trace_id 缺省时取当前活动 trace，使工具调用链可串联到所属请求。
    """
    tid = trace_id or _trace_id.get() or ""
    latency = _latency(t0)
    try:
        db.execute(
            "INSERT INTO tool_routing (id, trace_id, session_id, user_id, tool_name, namespace,"
            " routed_by, args_json, risk_level, schema_valid, permission_ok, risk_blocked,"
            " rate_limited, allowed, blocked_code, status, success, schema_tokens, latency_ms,"
            " error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()), tid, session_id or "", user_id or "", tool_name or "",
                namespace or "", routed_by or "",
                json.dumps(args or {}, ensure_ascii=False)[:4000],
                risk_level or "low",
                1 if schema_valid else 0, 1 if permission_ok else 0,
                1 if risk_blocked else 0, 1 if rate_limited else 0,
                1 if allowed else 0, (blocked_code or "")[:64],
                status or "ok", 1 if success else 0, int(schema_tokens or 0),
                latency, (error or "")[:500], _now(),
            ),
        )
    except Exception as e:
        logger.warning("tool_routing 落库失败: %s", e)
    logger.info(
        "[TOOL_ROUTING] trace=%s tool=%s ns=%s routed_by=%s status=%s allowed=%s "
        "schema_valid=%s permission_ok=%s latency=%dms schema_tokens=%d%s",
        tid or "-", tool_name or "-", namespace or "-", routed_by or "-", status,
        allowed, schema_valid, permission_ok, latency, int(schema_tokens or 0),
        f" blocked={blocked_code}" if blocked_code else "",
    )


def tool_routing_metrics(days: int = 7, range_mode: str = "days") -> dict:
    """聚合 Tool Routing 调用链五项核心指标 + 按工具/命名空间下钻 + 近期拦截事件。

    五项核心指标（req2）：
      - 参数合法率 param_validity_rate：schema_valid=1 占比
      - 任务成功率 task_success_rate：status=ok 且 success=1 占比（放行后执行成功率）
      - 工具 Schema Token schema_tokens：注入 LLM 的工具 schema token 总量/均值
      - 端到端延迟 e2e_latency：avg/p95（钩子+执行全链路）
      - 越权暴露率 privilege_exposure_rate：permission_ok=0 占比（越权尝试被拦截率）
    """
    since = _range_since(days, range_mode)
    total_row = db.query_one(
        "SELECT COUNT(*) AS n,"
        " COALESCE(SUM(schema_valid), 0) AS schema_valid_n,"
        " COALESCE(SUM(permission_ok = 0), 0) AS privilege_violations,"
        " COALESCE(SUM(allowed), 0) AS allowed_n,"
        " COALESCE(SUM(status = 'blocked'), 0) AS blocked_n,"
        " COALESCE(SUM(status = 'ok' AND success = 1), 0) AS success_n,"
        " COALESCE(SUM(status = 'error'), 0) AS error_n,"
        " COALESCE(SUM(risk_blocked), 0) AS risk_blocked_n,"
        " COALESCE(SUM(rate_limited), 0) AS rate_limited_n,"
        " COALESCE(SUM(schema_tokens), 0) AS schema_tokens_sum,"
        " COALESCE(AVG(latency_ms), 0) AS avg_latency"
        " FROM tool_routing WHERE created_at >= ?",
        (since,),
    ) or {}
    n = int(total_row.get("n", 0) or 0)
    allowed_n = int(total_row.get("allowed_n", 0) or 0)
    latencies = [
        r["latency_ms"] for r in db.query(
            "SELECT latency_ms FROM tool_routing WHERE created_at >= ? ORDER BY created_at DESC LIMIT 5000",
            (since,),
        )
    ]
    p95 = 0
    if latencies:
        latencies.sort()
        p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]

    by_tool = [
        {
            "tool": r["tool_name"],
            "namespace": r["namespace"],
            "total": int(r["total"]),
            "allowed": int(r["allowed_n"]),
            "blocked": int(r["blocked_n"]),
            "success": int(r["success_n"]),
            "errors": int(r["error_n"]),
            "param_validity_rate": round(int(r["schema_valid_n"]) / int(r["total"]), 4) if int(r["total"]) else 0.0,
            "task_success_rate": round(int(r["success_n"]) / int(r["allowed_n"]), 4) if int(r["allowed_n"]) else 0.0,
            "privilege_exposure_rate": round(int(r["privilege_violations"]) / int(r["total"]), 4) if int(r["total"]) else 0.0,
            "avg_latency_ms": int(r["avg_latency"] or 0),
            "schema_tokens": int(r["schema_tokens_sum"]),
        }
        for r in db.query(
            "SELECT tool_name, MAX(namespace) AS namespace, COUNT(*) AS total,"
            " COALESCE(SUM(allowed), 0) AS allowed_n,"
            " COALESCE(SUM(status = 'blocked'), 0) AS blocked_n,"
            " COALESCE(SUM(schema_valid), 0) AS schema_valid_n,"
            " COALESCE(SUM(permission_ok = 0), 0) AS privilege_violations,"
            " COALESCE(SUM(status = 'ok' AND success = 1), 0) AS success_n,"
            " COALESCE(SUM(status = 'error'), 0) AS error_n,"
            " COALESCE(SUM(schema_tokens), 0) AS schema_tokens_sum,"
            " COALESCE(AVG(latency_ms), 0) AS avg_latency"
            " FROM tool_routing WHERE created_at >= ? GROUP BY tool_name ORDER BY total DESC",
            (since,),
        )
    ]

    by_namespace = [
        {
            "namespace": r["namespace"] or "(未分组)",
            "total": int(r["total"]),
            "blocked": int(r["blocked_n"]),
            "success": int(r["success_n"]),
        }
        for r in db.query(
            "SELECT namespace, COUNT(*) AS total,"
            " COALESCE(SUM(status = 'blocked'), 0) AS blocked_n,"
            " COALESCE(SUM(status = 'ok' AND success = 1), 0) AS success_n"
            " FROM tool_routing WHERE created_at >= ? GROUP BY namespace ORDER BY total DESC",
            (since,),
        )
    ]

    # 近期被拦截/失败的调用链事件（供前端 drill-down 定位「哪次调用被哪个钩子拦截」）
    blocked_events = [
        {
            "tool_name": r["tool_name"],
            "namespace": r["namespace"],
            "routed_by": r["routed_by"],
            "blocked_code": r["blocked_code"],
            "status": r["status"],
            "risk_level": r["risk_level"],
            "user_id": r["user_id"],
            "error": r["error"],
            "created_at": r["created_at"],
        }
        for r in db.query(
            "SELECT tool_name, namespace, routed_by, blocked_code, status, risk_level,"
            " user_id, error, created_at FROM tool_routing"
            " WHERE created_at >= ? AND (status = 'blocked' OR status = 'error')"
            " ORDER BY created_at DESC LIMIT 30",
            (since,),
        )
    ]

    return {
        "days": days,
        "overview": {
            "total_calls": n,
            "allowed": allowed_n,
            "blocked": int(total_row.get("blocked_n", 0) or 0),
            "success": int(total_row.get("success_n", 0) or 0),
            "errors": int(total_row.get("error_n", 0) or 0),
            # 五项核心指标
            "param_validity_rate": round(int(total_row.get("schema_valid_n", 0) or 0) / n, 4) if n else 0.0,
            "task_success_rate": round(int(total_row.get("success_n", 0) or 0) / allowed_n, 4) if allowed_n else 0.0,
            "privilege_exposure_rate": round(int(total_row.get("privilege_violations", 0) or 0) / n, 4) if n else 0.0,
            "schema_tokens_total": int(total_row.get("schema_tokens_sum", 0) or 0),
            "schema_tokens_avg": round(int(total_row.get("schema_tokens_sum", 0) or 0) / n, 1) if n else 0.0,
            "avg_latency_ms": int(total_row.get("avg_latency", 0) or 0),
            "p95_latency_ms": int(p95),
            # 约束闸命中分布
            "risk_blocked": int(total_row.get("risk_blocked_n", 0) or 0),
            "rate_limited": int(total_row.get("rate_limited_n", 0) or 0),
        },
        "by_tool": by_tool,
        "by_namespace": by_namespace,
        "blocked_events": blocked_events,
    }



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


def _range_since(days: int, range_mode: str = "days") -> str:
    """时间窗起点。

    range_mode='today'：仅今天（起点=今日 00:00），页面只呈现今日数据；
    其余（'days'/缺省）：按天数回溯（含今天），与 _since 同语义。
    """
    if (range_mode or "days") == "today":
        return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return _since(days)


def _range_days(days: int, range_mode: str) -> int:
    """today 模式下的有效天数（用于日均/趋势横轴），恒为 1。"""
    return 1 if (range_mode or "days") == "today" else max(1, days)


def personal_metrics(user_id: str, days: int = 7, range_mode: str = "days") -> dict:
    """仅聚合当前账号的业务指标；不读取正文、步骤、模型或工具数据。"""
    if not user_id or user_id in {"guest", "system", "-"}:
        raise ValueError("个人统计需要有效的登录用户")
    days = max(1, min(days, RETENTION_DAYS))
    eff_days = _range_days(days, range_mode)
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=eff_days - 1)
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
    daily = {(start + timedelta(days=i)).date().isoformat(): empty() for i in range(eff_days)}
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


def personal_context_overview(user_id: str, days: int = 7, range_mode: str = "days") -> dict:
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
    since = _range_since(days, range_mode)

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


def personal_session_contexts(user_id: str, days: int = 7, limit: int = 20,
                              range_mode: str = "days") -> dict:
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
    since = _range_since(days, range_mode)
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


def session_observability(session_id: str, day: str = "", trace_id: str = "") -> dict:
    """单个对话窗口的统一可观测视图（需求2：上下文构成 + 请求追踪 + 调用链 合一）。

    同一个对话（session）下提供三种视角，前端以 tab 切换：
      - 上下文构成：选定 trace 的各语义段 token 构成（与聊天页缩略图同口径）；
      - 请求追踪：该会话全部 trace 列表（可按 day 过滤），即 Traces 的会话级切片；
      - 调用链：选定 trace 的 span 序列（llm/tool/step），复用 get_trace 口径。
    另附该会话的记忆注入概况（memory/memory_profile/memory_relevant 段累计）与
    所属用户的记忆总览，使「上下文与记忆」可按对话/按用户查看而非仅全局累计。

    Args:
        session_id: 会话 id
        day: 可选 'YYYY-MM-DD'，仅返回该天的 trace；空=全部
        trace_id: 可选，指定查看某次请求的上下文/调用链；空=取（该天）最近一次
    """
    empty = {
        "session_id": session_id, "title": "", "user_id": "", "username": "",
        "days": [], "traces": [], "selected_trace_id": "", "segments": [],
        "total_tokens": 0, "spans": [], "memory": None, "user_memory": None,
    }
    if not session_id:
        return empty
    meta = db.query_one(
        "SELECT session_id, user_id, title FROM chat_sessions WHERE session_id = ?",
        (session_id,),
    )
    params: list = [session_id]
    day_filter = ""
    if day:
        day_filter = " AND date(started_at) = ?"
        params.append(day)
    traces = [
        {
            "trace_id": r["id"], "agent": r["agent"], "status": r["status"],
            "input": (r["input"] or "").replace("\n", " ")[:60],
            "prompt_tokens": int(r["prompt_tokens"] or 0),
            "latency_ms": int(r["latency_ms"] or 0),
            "started_at": r["started_at"], "day": (r["started_at"] or "")[:10],
        }
        for r in db.query(
            "SELECT id, agent, status, input, prompt_tokens, latency_ms, started_at"
            " FROM agent_traces WHERE session_id = ? AND status != 'running'" + day_filter +
            " ORDER BY started_at DESC LIMIT 100",
            tuple(params),
        )
    ]
    days = sorted({t["day"] for t in traces if t["day"]}, reverse=True)
    selected = None
    if trace_id:
        selected = next((t for t in traces if t["trace_id"] == trace_id), None)
    if not selected and traces:
        selected = traces[0]
    sel_id = selected["trace_id"] if selected else ""

    segments = []
    total_tokens = 0
    if sel_id:
        rows = db.query(
            "SELECT segment, COALESCE(SUM(tokens), 0) AS tokens"
            " FROM agent_token_segments WHERE trace_id = ? GROUP BY segment ORDER BY tokens DESC",
            (sel_id,),
        )
        seg_total = sum(int(r["tokens"]) for r in rows)
        total_tokens = int(selected["prompt_tokens"]) or seg_total
        segments = [
            {
                "segment": r["segment"],
                "label": SEGMENT_LABELS.get(r["segment"], r["segment"]),
                "tokens": int(r["tokens"]),
                "share": round(int(r["tokens"]) / seg_total, 4) if seg_total else 0.0,
            }
            for r in rows
        ]

    spans = []
    if sel_id:
        trace = get_trace(sel_id)
        spans = (trace or {}).get("spans", []) or []

    # 该会话的记忆注入概况：memory 系 segment 跨 trace 累计（按对话查看记忆）
    mem_rows = db.query(
        "SELECT segment, COALESCE(SUM(tokens), 0) AS tokens, COUNT(*) AS n"
        " FROM agent_token_segments WHERE trace_id IN"
        " (SELECT id FROM agent_traces WHERE session_id = ?)"
        " AND segment IN ('memory', 'memory_profile', 'memory_relevant')"
        " GROUP BY segment ORDER BY tokens DESC",
        (session_id,),
    )
    memory = {
        "segments": [
            {"segment": r["segment"], "label": SEGMENT_LABELS.get(r["segment"], r["segment"]),
             "tokens": int(r["tokens"]), "count": int(r["n"])}
            for r in mem_rows
        ],
        "total_tokens": sum(int(r["tokens"]) for r in mem_rows),
    }
    user_memory = None
    uid = (meta or {}).get("user_id") or ""
    if uid and uid not in _GUEST_USER_IDS:
        try:
            from app.services.memory import MemoryService
            user_memory = MemoryService.usage_overview(uid)
        except Exception:
            user_memory = None

    uname = _resolve_usernames({uid}).get(uid, uid)
    return {
        "session_id": session_id,
        "title": (meta or {}).get("title") or "新对话",
        "user_id": uid,
        "username": uname,
        "days": days,
        "traces": traces,
        "selected_trace_id": sel_id,
        "selected_at": (selected or {}).get("started_at", ""),
        "segments": segments,
        "total_tokens": total_tokens,
        "spans": spans,
        "memory": memory,
        "user_memory": user_memory,
    }


def metrics(days: int = 7, range_mode: str = "days") -> dict:
    """聚合指标：总览 + 每日趋势 + 按 Agent + 按工具（Token/延迟/成功率/调用次数）。

    range_mode='today' 时仅统计今日；并额外输出 golden_signals（对齐主流可观测性
    四大黄金信号：流量 Rate / 错误 Errors / 延迟 Duration / 饱和度 Saturation）。
    """
    since = _range_since(days, range_mode)
    eff_days = _range_days(days, range_mode)
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
    finished_safe = finished or 0
    golden = {
        # 流量 Rate：窗口内请求吞吐（req/day）
        "rate_per_day": round(finished_safe / eff_days, 1) if eff_days else 0.0,
        # 错误 Errors：失败占比（error+timeout+interrupted / 完成数）
        "error_rate": round(
            (int(overview.get("errors", 0)) + int(overview.get("timeout", 0))
             + int(overview.get("interrupted", 0))) / finished_safe, 4
        ) if finished_safe else 0.0,
        # 延迟 Duration：avg + p95
        "duration": {
            "avg_ms": int(overview.get("avg_latency", 0)),
            "p95_ms": int(p95),
        },
        # 饱和度 Saturation：上下文/Token 水位相对告警阈值的占用
        "saturation": {
            "avg_tokens_per_request": avg_tokens,
            "warn_threshold": warn_threshold,
            "utilization": round(avg_tokens / warn_threshold, 4) if warn_threshold else 0.0,
            "overuse": bool(finished_safe and avg_tokens > warn_threshold),
        },
    }
    return {
        "days": days,
        "range": range_mode or "days",
        "golden_signals": golden,
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


def monitor_metrics(days: int = 7, range_mode: str = "days") -> dict:
    """指标监控四方面聚合：质量 / 安全 / 业务（性能方面复用 metrics() 口径）。

    - 质量：点赞点踩反馈、最近回归运行均分、Skill 预审风险分布
    - 安全：网关安全事件 + 工具拦截环节计数
    - 业务：资产存量 + 活跃用户 + usage_daily 用量趋势（Token 用量融合于此）
    """
    since = _range_since(days, range_mode)
    day_since = since[:10]
    fb = db.query_one(
        "SELECT COALESCE(SUM(rating = 'like'), 0) AS likes,"
        " COALESCE(SUM(rating = 'dislike'), 0) AS dislikes"
        " FROM message_feedbacks WHERE created_at >= ?", (since,)) or {}
    eval_runs = [dict(r) for r in db.query(
        "SELECT r.id, r.dataset_id, r.status, r.started_at, d.name AS dataset_name"
        " FROM eval_runs r LEFT JOIN eval_datasets d ON d.id = r.dataset_id"
        " WHERE r.status = 'done' AND r.started_at >= ?"
        " ORDER BY r.started_at DESC LIMIT 5", (since,))]
    for run in eval_runs:
        agg = db.query_one(
            "SELECT COALESCE(AVG(answer_relevance), 0) AS answer_relevance,"
            " COALESCE(AVG(context_precision), 0) AS context_precision,"
            " COALESCE(AVG(faithfulness), 0) AS faithfulness,"
            " COALESCE(AVG(judge_score), 0) AS judge_score,"
            " COALESCE(SUM(retrieval_hit), 0) AS hits, COUNT(*) AS total"
            " FROM eval_results WHERE run_id = ?", (run["id"],)) or {}
        run["avg"] = dict(agg)
    reviews = db.query_one(
        "SELECT COUNT(*) AS total, COALESCE(SUM(risk = 'high'), 0) AS high,"
        " COALESCE(SUM(risk = 'unsupported'), 0) AS unsupported"
        " FROM skill_review_logs WHERE created_at >= ?", (since,)) or {}
    events = db.query_one(
        "SELECT COUNT(*) AS total, COALESCE(SUM(status = 'pending'), 0) AS pending,"
        " COALESCE(SUM(risk_level = 'high'), 0) AS high,"
        " COALESCE(SUM(risk_level = 'critical'), 0) AS critical"
        " FROM gateway_security_events WHERE created_at >= ?", (since,)) or {}
    routing = tool_routing_metrics(days, range_mode)
    counts = db.query_one(
        "SELECT (SELECT COUNT(*) FROM chat_sessions WHERE created_at >= ?) AS sessions,"
        " (SELECT COUNT(*) FROM chat_messages WHERE created_at >= ?) AS messages,"
        " (SELECT COUNT(DISTINCT user_id) FROM usage_daily WHERE day >= ?) AS active_users,"
        " (SELECT COUNT(*) FROM upload_records WHERE created_at >= ?) AS uploads,"
        " (SELECT COUNT(*) FROM knowledge_bases WHERE created_at >= ?) AS knowledge_bases,"
        " (SELECT COUNT(*) FROM wiki_pages WHERE created_at >= ?) AS wiki_pages,"
        " (SELECT COUNT(*) FROM user_memories WHERE created_at >= ?) AS memories",
        (since, since, day_since, since, since, since, since)) or {}
    usage_daily = [dict(r) for r in db.query(
        "SELECT day, COALESCE(SUM(query_count), 0) AS queries,"
        " COALESCE(SUM(prompt_chars), 0) AS prompt_chars,"
        " COALESCE(SUM(completion_chars), 0) AS completion_chars"
        " FROM usage_daily WHERE day >= ? GROUP BY day ORDER BY day", (day_since,))]
    return {
        "quality": {
            "likes": fb.get("likes", 0),
            "dislikes": fb.get("dislikes", 0),
            "eval_runs": eval_runs,
            "skill_reviews": {
                "total": reviews.get("total", 0),
                "high": reviews.get("high", 0),
                "unsupported": reviews.get("unsupported", 0),
            },
        },
        "security": {
            "events_total": events.get("total", 0),
            "pending": events.get("pending", 0),
            "high": events.get("high", 0),
            "critical": events.get("critical", 0),
            "tool_blocked": len(routing.get("blocked_events") or []),
        },
        "business": {
            "counts": dict(counts or {}),
            "usage_daily": usage_daily,
        },
    }


def tool_chains(days: int = 7, range_mode: str = "days", limit: int = 50) -> dict:
    """工具调用链路：按 trace 聚合工具 span（调用序列 / 失败数 / 总耗时）。"""
    since = _range_since(days, range_mode)
    rows = db.query(
        "SELECT s.trace_id, COUNT(*) AS tool_count,"
        " COALESCE(SUM(s.status = 'error'), 0) AS error_count,"
        " COALESCE(SUM(s.latency_ms), 0) AS latency_ms,"
        " (SELECT GROUP_CONCAT(x.name, ' → ') FROM"
        "   (SELECT name FROM agent_spans WHERE trace_id = s.trace_id AND kind = 'tool' ORDER BY seq) x"
        " ) AS tools"
        " FROM agent_spans s JOIN agent_traces t ON t.id = s.trace_id"
        " WHERE s.kind = 'tool' AND t.started_at >= ?"
        " GROUP BY s.trace_id ORDER BY MAX(t.started_at) DESC LIMIT ?", (since, max(1, limit)))
    items = []
    for row in rows:
        trace = db.query_one(
            "SELECT id, agent, status, latency_ms, started_at, user_id, input"
            " FROM agent_traces WHERE id = ?", (row["trace_id"],))
        if not trace:
            continue
        items.append({
            **dict(row),
            "agent": trace["agent"],
            "status": trace["status"],
            "started_at": trace["started_at"],
            "trace_latency_ms": trace["latency_ms"],
            "input": trace["input"],
            "user_id": trace["user_id"],
        })
    mapping = _resolve_usernames([it["user_id"] for it in items])
    for item in items:
        _attach_username(item, mapping)
    return {"items": items}


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
        db.execute("DELETE FROM tool_routing WHERE created_at < ?", (cutoff,))
        n = db.execute("DELETE FROM agent_traces WHERE started_at < ?", (cutoff,))
        if n:
            logger.info("已清理 %d 条过期 Agent trace（保留 %d 天）", n, days)
        return n
    except Exception as e:
        logger.warning("清理过期 trace 失败: %s", e)
        return 0
