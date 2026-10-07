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
    """写入一条网关调用日志，返回 log_id。entry 字段见 gateway_logs 表结构。"""
    log_id = entry.get("id") or str(uuid.uuid4())
    try:
        db.execute(
            "INSERT INTO gateway_logs (id, key_id, key_name, model_requested, model_mapped,"
            " channel_id, channel_name, upstream_url, stream, status_code, status,"
            " prompt_tokens, completion_tokens, total_tokens, latency_ms, retries,"
            " tool_calls, request_body, response_preview, risk_level, risk_score, risks,"
            " audit_action, trace_steps, client_ip, error, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                log_id,
                entry.get("key_id", ""), entry.get("key_name", ""),
                entry.get("model_requested", ""), entry.get("model_mapped", ""),
                entry.get("channel_id", ""), entry.get("channel_name", ""),
                entry.get("upstream_url", ""), 1 if entry.get("stream") else 0,
                int(entry.get("status_code") or 0), entry.get("status", "ok"),
                int(entry.get("prompt_tokens") or 0), int(entry.get("completion_tokens") or 0),
                int(entry.get("total_tokens") or 0), int(entry.get("latency_ms") or 0),
                int(entry.get("retries") or 0),
                _dumps(entry.get("tool_calls") or []),
                _clip(entry.get("request_body", ""), _MAX_BODY),
                _clip(entry.get("response_preview", ""), _MAX_PREVIEW),
                entry.get("risk_level", "none"), int(entry.get("risk_score") or 0),
                _dumps(entry.get("risks") or []), entry.get("audit_action", ""),
                _dumps(entry.get("trace_steps") or []), entry.get("client_ip", ""),
                _clip(entry.get("error", ""), 500), entry.get("created_at") or _now(),
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
    })
    return d


def query_logs(key_id: str = "", model: str = "", channel_id: str = "", status: str = "",
               risk_level: str = "", keyword: str = "", start: str = "", end: str = "",
               limit: int = 30, offset: int = 0) -> dict:
    """搜索/筛选/分页查询日志列表，返回 {total, items}。"""
    where = ["1=1"]
    params: list = []
    if key_id:
        where.append("key_id = ?"); params.append(key_id)
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
        f"SELECT * FROM gateway_logs WHERE {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
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
        " COALESCE(SUM(total_tokens),0) AS tokens,"
        " COALESCE(AVG(latency_ms),0) AS avg_latency,"
        " COALESCE(SUM(risk_level!='none'),0) AS risky"
        " FROM gateway_logs WHERE created_at >= ?", (since,),
    ) or {}
    total = int(overview.get("total", 0))
    daily = [dict(r) for r in db.query(
        "SELECT date(created_at) AS day, COUNT(*) AS total,"
        " COALESCE(SUM(status='ok'),0) AS ok, COALESCE(SUM(status='error'),0) AS errors,"
        " COALESCE(SUM(status='blocked'),0) AS blocked,"
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
        " COALESCE(SUM(total_tokens),0) AS tokens"
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
    return {
        "days": days,
        "overview": {
            "total": total,
            "ok": int(overview.get("ok", 0)),
            "errors": int(overview.get("errors", 0)),
            "blocked": int(overview.get("blocked", 0)),
            "risky": int(overview.get("risky", 0)),
            "tokens": int(overview.get("tokens", 0)),
            "avg_latency_ms": int(overview.get("avg_latency", 0)),
            "success_rate": round(int(overview.get("ok", 0)) / total, 4) if total else 0.0,
        },
        "daily": daily, "by_channel": by_channel, "by_model": by_model,
        "by_risk": by_risk, "top_keys": top_keys,
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
