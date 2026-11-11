"""
见字如面 - 模型网关安全审计「风险事件」闭环

代理链路（proxy.prepare）在安全审计命中风险时调用 record_event 落一条事件，
关联 gateway_logs（log_id）。安全审计中心（前端 SecurityPanel）轮询未确认事件
弹窗告警；每条事件可：
  - 由模型生成处理策略建议（generate_suggestion → 落 suggestion 字段）；
  - 运维处理后记录 status/resolution/resolved_by/resolved_at/resolved_note 留痕。

事件状态：pending(待处理) → resolved(已处理)；acked 表示前端弹窗已被查看
（弹窗只弹未 acked 的事件，避免重复打扰；处理后自动 acked）。

设计原则：任何异常都静默降级，绝不因事件落库/建议生成失败阻断代理主流程。
"""
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from app.core import db
from app.services.gateway import security

logger = logging.getLogger("jianziruyang.gateway.security_events")

# 事件状态
EVENT_STATUS = ("pending", "resolved")
# 处理动作枚举（供前端下拉；模型建议也会给出推荐动作）
RESOLUTIONS = ("masked", "blocked", "whitelisted", "ignored", "investigated", "other")
RESOLUTION_LABELS = {
    "masked": "已脱敏放行",
    "blocked": "已阻断",
    "whitelisted": "已加白名单",
    "ignored": "确认误报·忽略",
    "investigated": "已排查处置",
    "other": "其他",
}


def _now() -> str:
    return datetime.now().isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _dumps(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return "[]" if isinstance(obj, list) else "{}"


def _parse_json(raw, fallback):
    try:
        val = json.loads(raw or "")
        return val if isinstance(val, (list, dict)) else fallback
    except (json.JSONDecodeError, TypeError):
        return fallback


def _severity_ge(level: str, min_level: str) -> bool:
    """风险等级是否 >= 告警阈值（用于决定是否弹窗）。"""
    order = security.SEVERITY_ORDER
    return order.get(level, 0) >= order.get(min_level, 0)


# ---------------------------------------------------------------------------
# 记录（代理链路调用）
# ---------------------------------------------------------------------------

def record_event(*, log_id: str = "", client_ip: str = "", user_id: str = "",
                 role: str = "", model: str = "", key_name: str = "",
                 risk_level: str = "none", risk_score: int = 0,
                 findings: Optional[list] = None, audit_action: str = "",
                 settings: Optional[dict] = None) -> str:
    """命中风险即落一条安全事件，返回 event_id（失败静默返回空串）。

    findings 去重合并后入库；是否弹窗（前端据 acked + severity 阈值判定）不在此处决策，
    仅完整留存事件，阈值过滤交给 list_events(alert_only=True)。
    """
    try:
        event_id = _new_id()
        db.execute(
            "INSERT INTO gateway_security_events (id, log_id, created_at, client_ip, user_id,"
            " role, model, key_name, risk_level, risk_score, findings, audit_action,"
            " status, acked, suggestion, suggested_at, resolution, resolved_by,"
            " resolved_at, resolved_note)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'pending', 0, '', '', '', '', '', '')",
            (event_id, log_id or "", _now(), client_ip or "", user_id or "",
             role or "", model or "", key_name or "", risk_level or "none",
             int(risk_score or 0), _dumps(findings or []), audit_action or ""),
        )
        return event_id
    except Exception as e:
        logger.warning("安全事件记录失败（不阻断请求）: %s", e)
        return ""


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def _row_to_dict(row: dict) -> dict:
    findings = _parse_json(row.get("findings"), [])
    return {
        "id": row["id"],
        "log_id": row.get("log_id") or "",
        "created_at": row.get("created_at") or "",
        "client_ip": row.get("client_ip") or "",
        "user_id": row.get("user_id") or "",
        "role": row.get("role") or "",
        "model": row.get("model") or "",
        "key_name": row.get("key_name") or "",
        "risk_level": row.get("risk_level") or "none",
        "risk_score": int(row.get("risk_score") or 0),
        "findings": findings,
        "finding_count": len(findings),
        "audit_action": row.get("audit_action") or "",
        "status": row.get("status") or "pending",
        "acked": bool(row.get("acked")),
        "suggestion": row.get("suggestion") or "",
        "suggested_at": row.get("suggested_at") or "",
        "resolution": row.get("resolution") or "",
        "resolution_label": RESOLUTION_LABELS.get(row.get("resolution") or "", ""),
        "resolved_by": row.get("resolved_by") or "",
        "resolved_at": row.get("resolved_at") or "",
        "resolved_note": row.get("resolved_note") or "",
    }


def get_event(event_id: str) -> Optional[dict]:
    row = db.query_one("SELECT * FROM gateway_security_events WHERE id=?", (event_id,))
    return _row_to_dict(row) if row else None


def list_events(status: str = "", limit: int = 50, offset: int = 0,
                alert_only: bool = False, min_severity: str = "medium") -> dict:
    """事件列表（分页）。

    - status：'' 全部 | 'pending' | 'resolved'；
    - alert_only=True：只返回「需弹窗告警」的事件——pending 且未 acked 且
      risk_level >= min_severity（前端轮询用）；
    返回 {events, total, pending, unacked_alerts}。
    """
    where, params = [], []
    if status in EVENT_STATUS:
        where.append("status=?")
        params.append(status)
    if alert_only:
        where.append("status='pending' AND acked=0")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = int((db.query_one(
        f"SELECT COUNT(*) AS n FROM gateway_security_events{clause}", tuple(params)) or {}).get("n") or 0)
    rows = db.query(
        f"SELECT * FROM gateway_security_events{clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        tuple(params) + (max(1, min(200, int(limit))), max(0, int(offset))))
    events = [_row_to_dict(r) for r in rows]
    if alert_only:
        events = [e for e in events if _severity_ge(e["risk_level"], min_severity)]
    pending = int((db.query_one(
        "SELECT COUNT(*) AS n FROM gateway_security_events WHERE status='pending'") or {}).get("n") or 0)
    unacked = db.query(
        "SELECT risk_level FROM gateway_security_events WHERE status='pending' AND acked=0")
    unacked_alerts = sum(1 for r in unacked if _severity_ge(r.get("risk_level") or "none", min_severity))
    return {"events": events, "total": total, "pending": pending,
            "unacked_alerts": unacked_alerts}


def ack_events(event_ids: Optional[List[str]] = None) -> int:
    """确认（弹窗已查看）：event_ids 为空则确认全部未 acked 的 pending 事件。"""
    if event_ids:
        marks = ",".join("?" for _ in event_ids)
        return db.execute(
            f"UPDATE gateway_security_events SET acked=1 WHERE id IN ({marks}) AND acked=0",
            tuple(str(e) for e in event_ids))
    return db.execute(
        "UPDATE gateway_security_events SET acked=1 WHERE status='pending' AND acked=0")


# ---------------------------------------------------------------------------
# 模型建议（处理策略）
# ---------------------------------------------------------------------------

_SUGGEST_ROLE = "assistant"


def _build_prompt(event: dict) -> str:
    findings = event.get("findings") or []
    lines = []
    for f in findings[:12]:
        matched = (f.get("matched") or "")[:120]
        lines.append(f"- [{f.get('severity')}] {f.get('rule_name')}"
                     f"（{f.get('category_label')}）命中：{matched}")
    detail = "\n".join(lines) or "（无明细）"
    return (
        "你是模型网关的安全审计助手。网关在一次 LLM 请求中检测到以下风险，"
        "请给出处置建议。\n\n"
        f"风险等级：{event.get('risk_level')}（{event.get('risk_score')} 分）\n"
        f"业务场景(role)：{event.get('role') or '-'}\n"
        f"请求模型：{event.get('model') or '-'}\n"
        f"审计动作：{event.get('audit_action') or '仅记录'}\n"
        f"命中明细：\n{detail}\n\n"
        "请用简洁中文输出：1) 风险研判（是否真实威胁/可能误报）；"
        "2) 推荐处理动作（从 已脱敏放行/已阻断/已加白名单/确认误报忽略/已排查处置 中选一并说明理由）；"
        "3) 后续加固建议（1~3 条）。控制在 200 字以内。"
    )


async def generate_suggestion(event_id: str) -> dict:
    """由模型生成处理策略建议并落库；失败降级为规则化建议（不抛异常）。"""
    event = get_event(event_id)
    if not event:
        return {"success": False, "error": "事件不存在"}
    prompt = _build_prompt(event)
    suggestion = ""
    try:
        from app.core.llm import LLMFactory
        llm = LLMFactory.create_for_role(_SUGGEST_ROLE)
        collected = ""
        async for chunk in llm.chat([{"role": "user", "content": prompt}], stream=False):
            collected += chunk
        suggestion = collected.strip()
    except Exception as e:
        logger.warning("模型建议生成失败，降级规则化建议: %s", e)
    if not suggestion:
        suggestion = _fallback_suggestion(event)
    db.execute(
        "UPDATE gateway_security_events SET suggestion=?, suggested_at=? WHERE id=?",
        (suggestion[:4000], _now(), event_id))
    return {"success": True, "event": get_event(event_id)}


def _fallback_suggestion(event: dict) -> str:
    """模型不可用时的规则化建议（保证闭环不断）。"""
    level = event.get("risk_level") or "none"
    findings = event.get("findings") or []
    cats = {f.get("category") for f in findings}
    tips = []
    if "credential_leak" in cats:
        tips.append("检测到疑似凭证：建议立即阻断并轮换相关密钥，核查来源是否日志/前端误传。")
    if "unicode_steg" in cats:
        tips.append("检测到 Unicode 隐写字符：建议开启脱敏模式剔除隐写字符后再转发，警惕提示注入。")
    if "tool_command" in cats:
        tips.append("检测到工具命令外联：建议阻断，确认是否为越权命令注入。")
    if "outbound" in cats:
        tips.append("检测到外联追踪/公网探测：建议加白名单或阻断，核查数据外泄风险。")
    if "sensitive_path" in cats:
        tips.append("检测到敏感路径访问：建议阻断并排查是否为路径穿越攻击。")
    action = "已阻断" if level in ("high", "critical") else "已脱敏放行"
    head = f"【规则化建议】风险等级 {level}，推荐处理动作：{action}。\n"
    return head + ("\n".join(tips) if tips else "建议结合请求上下文研判后处置。")


# ---------------------------------------------------------------------------
# 处理留痕
# ---------------------------------------------------------------------------

def resolve_event(event_id: str, *, resolution: str = "", note: str = "",
                  resolved_by: str = "") -> Optional[dict]:
    """运维处理事件并留痕（status→resolved，自动 acked）。"""
    cur = get_event(event_id)
    if not cur:
        return None
    resolution = (resolution or "").strip()
    if resolution and resolution not in RESOLUTIONS:
        raise ValueError(f"不支持的处理动作：{resolution}（可选 {'/'.join(RESOLUTIONS)}）")
    db.execute(
        "UPDATE gateway_security_events SET status='resolved', resolution=?, resolved_note=?,"
        " resolved_by=?, resolved_at=?, acked=1 WHERE id=?",
        (resolution, (note or "")[:1000], resolved_by or "", _now(), event_id))
    return get_event(event_id)


def reopen_event(event_id: str) -> Optional[dict]:
    """重新打开已处理事件（回到 pending，清空处理留痕但保留建议）。"""
    cur = get_event(event_id)
    if not cur:
        return None
    db.execute(
        "UPDATE gateway_security_events SET status='pending', resolution='', resolved_note=?,"
        " resolved_by='', resolved_at='', acked=0 WHERE id=?",
        ("", event_id))
    return get_event(event_id)


def delete_event(event_id: str) -> bool:
    return db.execute("DELETE FROM gateway_security_events WHERE id=?", (event_id,)) > 0


def purge_resolved(days: int = 30) -> int:
    """清理 N 天前已处理的事件（跟随日志保留策略）。"""
    cutoff = (datetime.now() - timedelta(days=max(0, int(days or 0)))).isoformat()
    return db.execute(
        "DELETE FROM gateway_security_events WHERE status='resolved' AND created_at < ?",
        (cutoff,))
