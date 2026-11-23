"""
见字如面 - 模型网关请求日志与审计留存

每次调用把 状态码 / Token 消耗 / 上游路由 / 工具调用 / 请求参数 / 风险明细 / 可视化链路
全部入库，支持按密钥、模型、渠道、状态、风险等级、时间与关键词搜索/筛选/分页，可展开
查看完整明细。日志为“只追加”写入，任何异常都静默降级，绝不影响代理主流程。
"""
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from app.core import db

logger = logging.getLogger("jianziruyang.gateway.logs")

# 单条日志中请求体/响应预览的入库上限（防超长撑爆 SQLite）
_MAX_BODY = 20000
_MAX_PREVIEW = 2000


def _now() -> str:
    return datetime.now().isoformat()


def _clip(text: str, limit: int) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit] + "...(截断)"


def _dumps(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return "[]" if isinstance(obj, list) else "{}"


def write_log(entry: dict) -> str:
    """写入一条网关调用日志，返回 log_id。entry 字段见 gateway_logs 表结构。

    归属字段（user_id/billing_user_id/trace_id/span_id/job_id/origin/
    attribution_source）由代理 prepare 注入；usage_event_id 幂等：同一用量
    事件重复落库直接返回已有 log_id，不重复计量。
    """
    log_id = entry.get("id") or str(uuid.uuid4())
    usage_event_id = str(entry.get("usage_event_id") or "")
    try:
        if usage_event_id:
            existing = db.query_one(
                "SELECT id FROM gateway_logs WHERE usage_event_id=?", (usage_event_id,))
            if existing:
                return existing["id"]
        db.execute(
            "INSERT INTO gateway_logs (id, key_id, key_name, model_requested, model_mapped,"
            " channel_id, channel_name, upstream_url, stream, status_code, status,"
            " prompt_tokens, completion_tokens, cached_tokens, total_tokens, latency_ms, retries,"
            " tool_calls, request_body, response_preview, risk_level, risk_score, risks,"
            " audit_action, trace_steps, client_ip, error, role, created_at,"
            " user_id, billing_user_id, trace_id, span_id, job_id, origin,"
            " attribution_source, usage_event_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO NOTHING",
            (
                log_id,
                entry.get("key_id", ""), entry.get("key_name", ""),
                entry.get("model_requested", ""), entry.get("model_mapped", ""),
                entry.get("channel_id", ""), entry.get("channel_name", ""),
                entry.get("upstream_url", ""), 1 if entry.get("stream") else 0,
                int(entry.get("status_code") or 0), entry.get("status", "ok"),
                int(entry.get("prompt_tokens") or 0), int(entry.get("completion_tokens") or 0),
                int(entry.get("cached_tokens") or 0),
                int(entry.get("total_tokens") or 0), int(entry.get("latency_ms") or 0),
                int(entry.get("retries") or 0),
                _dumps(entry.get("tool_calls") or []),
                _clip(entry.get("request_body", ""), _MAX_BODY),
                _clip(entry.get("response_preview", ""), _MAX_PREVIEW),
                entry.get("risk_level", "none"), int(entry.get("risk_score") or 0),
                _dumps(entry.get("risks") or []), entry.get("audit_action", ""),
                _dumps(entry.get("trace_steps") or []), entry.get("client_ip", ""),
                _clip(entry.get("error", ""), 500), entry.get("role", ""),
                entry.get("created_at") or _now(),
                entry.get("user_id", ""), entry.get("billing_user_id", ""),
                entry.get("trace_id", ""), entry.get("span_id", ""),
                entry.get("job_id", ""), entry.get("origin", ""),
                entry.get("attribution_source", ""), usage_event_id,
            ),
        )
    except Exception as e:
        logger.warning("网关日志写入失败: %s", e)
    return log_id


def _row_to_summary(row: dict) -> dict:
    """列表用摘要（不含大字段 request_body / risks / trace_steps）。"""
    return {
        "id": row["id"],
        "key_name": row.get("key_name") or "",
        "key_id": row.get("key_id") or "",
        "model_requested": row.get("model_requested") or "",
        "model_mapped": row.get("model_mapped") or "",
        "channel_name": row.get("channel_name") or "",
        "status": row.get("status") or "ok",
        "status_code": int(row.get("status_code") or 0),
        "total_tokens": int(row.get("total_tokens") or 0),
        "prompt_tokens": int(row.get("prompt_tokens") or 0),
        "completion_tokens": int(row.get("completion_tokens") or 0),
        "latency_ms": int(row.get("latency_ms") or 0),
        "retries": int(row.get("retries") or 0),
        "risk_level": row.get("risk_level") or "none",
        "risk_score": int(row.get("risk_score") or 0),
        "audit_action": row.get("audit_action") or "",
        "client_ip": row.get("client_ip") or "",
        # 业务场景（role）：记忆抽取/意图改写/聊天回答…，空=非业务路由发起
        "role": row.get("role") or "",
        # 调用归属：发起用户/计费用户/归属来源（空 user_id=未归属到具体用户）
        "user_id": row.get("user_id") or "",
        "billing_user_id": row.get("billing_user_id") or "",
        "attribution_source": row.get("attribution_source") or "",
        # 四分类（与 stats 口径一致）：列表 SELECT 带计算列时直接用，
        # 明细 SELECT * 无该列时按用户/来源推导
        "attribution": row.get("attribution") or (
            "user" if row.get("user_id") else
            {"system_internal": "system", "public_shared": "public"}.get(
                row.get("attribution_source") or "", "unattributed")),
        "created_at": row.get("created_at"),
    }


def get_log(log_id: str) -> Optional[dict]:
    """展开查看：完整明细（含请求体、风险、链路、工具调用）。"""
    row = db.query_one("SELECT * FROM gateway_logs WHERE id=?", (log_id,))
    if not row:
        return None
    d = _row_to_summary(row)
    d.update({
        "stream": bool(row.get("stream")),
        "upstream_url": row.get("upstream_url") or "",
        "channel_id": row.get("channel_id") or "",
        "request_body": row.get("request_body") or "",
        "response_preview": row.get("response_preview") or "",
        "error": row.get("error") or "",
        "tool_calls": json.loads(row.get("tool_calls") or "[]"),
        "risks": json.loads(row.get("risks") or "[]"),
        "trace_steps": json.loads(row.get("trace_steps") or "[]"),
        # 全链路关联：与可观测性 trace 同源（签名上下文注入）
        "trace_id": row.get("trace_id") or "",
        "span_id": row.get("span_id") or "",
        "job_id": row.get("job_id") or "",
        "origin": row.get("origin") or "",
        "usage_event_id": row.get("usage_event_id") or "",
    })
    return d


# 归属四分类（界面口径）：用户业务=有归属用户；真实系统=内部密钥；
# 公共游客=公共密钥且无用户；历史未归属=其余（存量无证据行/密钥已删除）。
_ATTR_CASE = (
    "CASE WHEN user_id <> '' THEN 'user'"
    " WHEN key_id IN (SELECT id FROM gateway_keys WHERE key_type='internal') THEN 'system'"
    " WHEN key_id IN (SELECT id FROM gateway_keys WHERE key_type='public') THEN 'public'"
    " ELSE 'unattributed' END"
)
# 缓存未命中输入 Token：厂商 usage 的 prompt_tokens 扣除前缀缓存命中量
# （cached_tokens 来自 prompt_tokens_details.cached_tokens / prompt_cache_hit_tokens）。
_UNCACHED_SUM = ("COALESCE(SUM(CASE WHEN prompt_tokens > cached_tokens"
                 " THEN prompt_tokens - cached_tokens ELSE 0 END),0)")
_UNCACHED_SUM_L = ("COALESCE(SUM(CASE WHEN l.prompt_tokens > l.cached_tokens"
                   " THEN l.prompt_tokens - l.cached_tokens ELSE 0 END),0)")
_ATTR_CLAUSES = {
    "user": "user_id <> ''",
    "system": "user_id = '' AND key_id IN (SELECT id FROM gateway_keys WHERE key_type='internal')",
    "public": "user_id = '' AND key_id IN (SELECT id FROM gateway_keys WHERE key_type='public')",
    "unattributed": ("user_id = '' AND key_id NOT IN"
                     " (SELECT id FROM gateway_keys WHERE key_type IN ('internal','public'))"),
}


def query_logs(key_id: str = "", model: str = "", channel_id: str = "", status: str = "",
               risk_level: str = "", keyword: str = "", start: str = "", end: str = "",
               role: str = "", user_id: str = "", attribution: str = "",
               limit: int = 30, offset: int = 0) -> dict:
    """搜索/筛选/分页查询日志列表，返回 {total, items}。

    user_id 精确筛选发起用户；attribution 按四分类筛选
    （user/system/public/unattributed）。
    """
    where = ["1=1"]
    params: list = []
    if key_id:
        where.append("key_id = ?"); params.append(key_id)
    if user_id:
        where.append("user_id = ?"); params.append(user_id)
    if attribution in _ATTR_CLAUSES:
        where.append(_ATTR_CLAUSES[attribution])
    if role:
        where.append("role = ?"); params.append(role)
    if model:
        where.append("(model_requested LIKE ? OR model_mapped LIKE ?)")
        params.extend([f"%{model}%", f"%{model}%"])
    if channel_id:
        where.append("channel_id = ?"); params.append(channel_id)
    if status:
        where.append("status = ?"); params.append(status)
    if risk_level and risk_level != "none":
        where.append("risk_level = ?"); params.append(risk_level)
    elif risk_level == "none":
        where.append("risk_level = 'none'")
    if start:
        where.append("created_at >= ?"); params.append(start)
    if end:
        where.append("created_at <= ?"); params.append(end)
    if keyword:
        where.append("(request_body LIKE ? OR response_preview LIKE ? OR key_name LIKE ?)")
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw])
    clause = " AND ".join(where)
    total = db.query_one(f"SELECT COUNT(*) AS n FROM gateway_logs WHERE {clause}", tuple(params))
    rows = db.query(
        f"SELECT *, {_ATTR_CASE} AS attribution FROM gateway_logs WHERE {clause}"
        " ORDER BY created_at DESC LIMIT ? OFFSET ?",
        tuple(params + [max(1, min(limit, 100)), max(0, offset)]),
    )
    return {"total": total["n"] if total else 0, "items": [_row_to_summary(r) for r in rows]}


def stats(days: int = 7) -> dict:
    """仪表盘统计：总览 + 每日趋势 + 按渠道 + 按模型 + 风险分布 + Top 密钥。"""
    since = (datetime.now() - timedelta(days=max(1, days) - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat()
    overview = db.query_one(
        "SELECT COUNT(*) AS total,"
        " COALESCE(SUM(status='ok'),0) AS ok,"
        " COALESCE(SUM(status='error'),0) AS errors,"
        " COALESCE(SUM(status='blocked'),0) AS blocked,"
        " COALESCE(SUM(status='cancelled'),0) AS cancelled,"
        " COALESCE(SUM(status='timeout'),0) AS timeout,"
        " COALESCE(SUM(total_tokens),0) AS tokens,"
        " COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens),0) AS completion_tokens,"
        " COALESCE(SUM(cached_tokens),0) AS cached_tokens,"
        f" {_UNCACHED_SUM} AS uncached_tokens,"
        " COALESCE(AVG(latency_ms),0) AS avg_latency,"
        " COALESCE(SUM(risk_level!='none'),0) AS risky"
        " FROM gateway_logs WHERE created_at >= ?", (since,),
    ) or {}
    total = int(overview.get("total", 0))
    latencies = sorted(r["latency_ms"] for r in db.query(
        "SELECT latency_ms FROM gateway_logs WHERE created_at >= ? ORDER BY created_at DESC LIMIT 5000", (since,)))
    p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else 0
    daily = [dict(r) for r in db.query(
        "SELECT date(created_at) AS day, COUNT(*) AS total,"
        " COALESCE(SUM(status='ok'),0) AS ok, COALESCE(SUM(status='error'),0) AS errors,"
        " COALESCE(SUM(status='blocked'),0) AS blocked,"
        " COALESCE(SUM(status='cancelled'),0) AS cancelled,"
        " COALESCE(SUM(status='timeout'),0) AS timeout,"
        " COALESCE(SUM(total_tokens),0) AS tokens"
        " FROM gateway_logs WHERE created_at >= ? GROUP BY day ORDER BY day", (since,))]
    by_channel = [dict(r) for r in db.query(
        "SELECT channel_name AS name, COUNT(*) AS total,"
        " COALESCE(SUM(status='ok'),0) AS ok, COALESCE(SUM(status='error'),0) AS errors,"
        " COALESCE(SUM(total_tokens),0) AS tokens, COALESCE(AVG(latency_ms),0) AS avg_latency"
        " FROM gateway_logs WHERE created_at >= ? AND channel_name!=''"
        " GROUP BY channel_name ORDER BY total DESC", (since,))]
    by_model = [dict(r) for r in db.query(
        "SELECT model_requested AS name, COUNT(*) AS total,"
        " COALESCE(SUM(total_tokens),0) AS tokens,"
        " COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens),0) AS completion_tokens,"
        " COALESCE(SUM(cached_tokens),0) AS cached_tokens,"
        f" {_UNCACHED_SUM} AS uncached_tokens"
        " FROM gateway_logs WHERE created_at >= ? AND model_requested!=''"
        " GROUP BY model_requested ORDER BY total DESC LIMIT 10", (since,))]
    by_risk = [dict(r) for r in db.query(
        "SELECT risk_level AS level, COUNT(*) AS total FROM gateway_logs"
        " WHERE created_at >= ? GROUP BY risk_level", (since,))]
    top_keys = [dict(r) for r in db.query(
        "SELECT key_name AS name, COUNT(*) AS total, COALESCE(SUM(total_tokens),0) AS tokens,"
        " COALESCE(SUM(risk_level!='none'),0) AS risky"
        " FROM gateway_logs WHERE created_at >= ? AND key_name!=''"
        " GROUP BY key_name ORDER BY total DESC LIMIT 10", (since,))]
    # Token 分析：按密钥的完整用量口径（prompt/completion/总量/请求数/风险数 + 密钥类型）
    by_key = [dict(r) for r in db.query(
        "SELECT l.key_id, l.key_name AS name, COALESCE(k.key_type, '') AS key_type,"
        " COUNT(*) AS total, COALESCE(SUM(l.prompt_tokens),0) AS prompt_tokens,"
        " COALESCE(SUM(l.completion_tokens),0) AS completion_tokens,"
        " COALESCE(SUM(l.cached_tokens),0) AS cached_tokens,"
        f" {_UNCACHED_SUM_L} AS uncached_tokens,"
        " COALESCE(SUM(l.total_tokens),0) AS tokens,"
        " COALESCE(SUM(l.risk_level!='none'),0) AS risky"
        " FROM gateway_logs l LEFT JOIN gateway_keys k ON k.id = l.key_id"
        " WHERE l.created_at >= ? AND l.key_name!=''"
        " GROUP BY l.key_id, l.key_name ORDER BY tokens DESC LIMIT 20", (since,))]
    # 业务路由维度：按 role（chat_answer/wiki_compile/memory_extract…）聚合用量，
    # 与密钥归属同源（后台任务已按发起人恢复 CallContext），供密钥用量分析下钻
    by_role = [dict(r) for r in db.query(
        "SELECT role AS name, COUNT(*) AS total,"
        " COALESCE(SUM(total_tokens),0) AS tokens,"
        " COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens),0) AS completion_tokens,"
        " COALESCE(SUM(cached_tokens),0) AS cached_tokens,"
        f" {_UNCACHED_SUM} AS uncached_tokens,"
        " COALESCE(SUM(user_id!=''),0) AS attributed"
        " FROM gateway_logs WHERE created_at >= ? AND role!=''"
        " GROUP BY role ORDER BY tokens DESC LIMIT 20", (since,))]
    # 归属统计：用户业务/真实系统/公共游客/历史未归属（不再笼统计入「系统内部调用」）
    by_attribution = [dict(r) for r in db.query(
        f"SELECT {_ATTR_CASE} AS category, COUNT(*) AS total,"
        " COALESCE(SUM(total_tokens),0) AS tokens,"
        " COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens),0) AS completion_tokens"
        " FROM gateway_logs WHERE created_at >= ? GROUP BY category", (since,))]
    by_user = [dict(r) for r in db.query(
        "SELECT l.user_id, COALESCE(u.username, '') AS username, COUNT(*) AS total,"
        " COALESCE(SUM(l.total_tokens),0) AS tokens,"
        " COALESCE(SUM(l.prompt_tokens),0) AS prompt_tokens,"
        " COALESCE(SUM(l.completion_tokens),0) AS completion_tokens"
        " FROM gateway_logs l LEFT JOIN users u ON u.id = l.user_id"
        " WHERE l.created_at >= ? AND l.user_id <> ''"
        " GROUP BY l.user_id ORDER BY tokens DESC LIMIT 20", (since,))]
    return {
        "days": days,
        "overview": {
            "total": total,
            "ok": int(overview.get("ok", 0)),
            "errors": int(overview.get("errors", 0)),
            "blocked": int(overview.get("blocked", 0)),
            "cancelled": int(overview.get("cancelled", 0)),
            "timeout": int(overview.get("timeout", 0)),
            "p95_latency_ms": int(p95),
            "risky": int(overview.get("risky", 0)),
            "tokens": int(overview.get("tokens", 0)),
            "prompt_tokens": int(overview.get("prompt_tokens", 0)),
            "completion_tokens": int(overview.get("completion_tokens", 0)),
            "cached_tokens": int(overview.get("cached_tokens", 0)),
            "uncached_tokens": int(overview.get("uncached_tokens", 0)),
            "avg_latency_ms": int(overview.get("avg_latency", 0)),
            "success_rate": round(int(overview.get("ok", 0)) / total, 4) if total else 0.0,
        },
        "daily": daily, "by_channel": by_channel, "by_model": by_model,
        "by_risk": by_risk, "top_keys": top_keys, "by_key": by_key,
        "by_role": by_role,
        "by_attribution": by_attribution, "by_user": by_user,
    }


def token_requests(days: int = 7, model: str = "", channel_name: str = "",
                   limit: int = 200, role: str = "") -> dict:
    """Token 分析钻取：某模型 / 某渠道（路由规则）/ 某业务角色在时间窗内每次请求的 Token 明细。

    供 Token 分析面板「按模型 / 按渠道 / 按业务路由」表格行点击钻取：返回该维度下逐条请求的
    时间/模型/渠道/密钥/业务角色/状态/延迟/输入/输出/缓存命中/缓存未命中/total Token。
    model / channel_name / role 三选一（同时传以 model > channel > role 优先），均按精确匹配
    （与 stats 的 GROUP BY 口径一致）；缺省或都空则返回空集。
    """
    days = max(1, min(days, 90))
    since = (datetime.now() - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat()
    model = (model or "").strip()
    channel_name = (channel_name or "").strip()
    role = (role or "").strip()
    if model:
        clause, params = "created_at >= ? AND model_requested = ?", (since, model)
    elif channel_name:
        clause, params = "created_at >= ? AND channel_name = ?", (since, channel_name)
    elif role:
        clause, params = "created_at >= ? AND role = ?", (since, role)
    else:
        return {"days": days, "model": "", "channel_name": "", "role": "",
                "count": 0, "tokens": 0, "requests": []}
    lim = max(1, min(limit, 500))
    rows = db.query(
        "SELECT id, created_at, model_requested, model_mapped, channel_name, key_name, role,"
        " status, status_code, latency_ms, prompt_tokens, completion_tokens, cached_tokens,"
        f" total_tokens, risk_level FROM gateway_logs WHERE {clause}"
        " ORDER BY created_at DESC LIMIT ?", params + (lim,))
    requests = [{
        "id": r["id"], "created_at": r.get("created_at"),
        "model_requested": r.get("model_requested") or "",
        "model_mapped": r.get("model_mapped") or "",
        "channel_name": r.get("channel_name") or "",
        "key_name": r.get("key_name") or "", "role": r.get("role") or "",
        "status": r.get("status") or "ok", "status_code": int(r.get("status_code") or 0),
        "latency_ms": int(r.get("latency_ms") or 0),
        "prompt_tokens": int(r.get("prompt_tokens") or 0),
        "completion_tokens": int(r.get("completion_tokens") or 0),
        "cached_tokens": int(r.get("cached_tokens") or 0),
        "uncached_tokens": max(0, int(r.get("prompt_tokens") or 0) - int(r.get("cached_tokens") or 0)),
        "total_tokens": int(r.get("total_tokens") or 0),
        "risk_level": r.get("risk_level") or "none",
    } for r in rows]
    return {
        "days": days, "model": model, "channel_name": channel_name, "role": role,
        "count": len(requests),
        "tokens": sum(x["total_tokens"] for x in requests),
        "requests": requests,
    }


def purge_old(days: int = 30) -> int:
    """清理过期日志（启动时调用）。"""
    cutoff = (datetime.now() - timedelta(days=max(1, days))).isoformat()
    try:
        n = db.execute("DELETE FROM gateway_logs WHERE created_at < ?", (cutoff,))
        if n:
            logger.info("已清理 %d 条过期网关日志（保留 %d 天）", n, days)
        return n
    except Exception as e:
        logger.warning("清理网关日志失败: %s", e)
        return 0
