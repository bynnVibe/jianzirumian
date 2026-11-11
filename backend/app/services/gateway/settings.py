"""
见字如面 - 模型网关全局配置

单行配置（id='default'）：审计模式、三类检测开关、失败重试次数、内置规则停用清单、
自定义黑白名单、日志保留天数，以及网关默认模型与系统内部出网密钥。
读取时对缺失字段回退默认值，保证向前兼容。
"""
import json
import logging
from datetime import datetime

from app.core import db
from app.services.gateway import security

logger = logging.getLogger("jianziruyang.gateway.settings")

_DEFAULTS = {
    "audit_mode": "audit",
    "unicode_detection": 1,
    "tool_cmd_detection": 1,
    "outbound_detection": 1,
    "retry_count": 2,
    "disabled_rules": [],
    "blacklist": [],
    "whitelist": [],
    "log_retention_days": 30,
    # 网关默认模型：业务路由未指定模型名时的兜底出网模型（空=回退 .env API_MODEL）
    "default_model": "",
    # 系统内部出网密钥明文（各模块经 /v1 出网自动携带；本地信任边界内保存）
    "internal_key": "",
    # 出网密钥明文映射 {key_id: 明文}：公共密钥/用户密钥由系统按用户解析后自动携带
    # （与 internal_key 同级信任边界；下游外部调用方仍只在创建时见到一次明文）
    "outbound_keys": {},
    # 安全审计事件弹窗告警的最低风险等级（low|medium|high|critical）
    "security_alert_min_severity": "medium",
}

_JSON_FIELDS = ("disabled_rules", "blacklist", "whitelist")
_DICT_FIELDS = ("outbound_keys",)
_ALERT_SEVERITIES = ("low", "medium", "high", "critical")


def _now() -> str:
    return datetime.now().isoformat()


def _parse_json(raw, fallback):
    try:
        val = json.loads(raw or "[]")
        return val if isinstance(val, list) else fallback
    except (json.JSONDecodeError, TypeError):
        return fallback


def _parse_json_obj(raw, fallback):
    """解析 JSON 对象字段（如 outbound_keys）；非 dict 时回退默认值。"""
    try:
        val = json.loads(raw or "{}")
        return val if isinstance(val, dict) else fallback
    except (json.JSONDecodeError, TypeError):
        return fallback



def get_settings() -> dict:
    """读取网关配置（缺失时惰性初始化默认行）。"""
    row = db.query_one("SELECT * FROM gateway_settings WHERE id = 'default'")
    if not row:
        db.execute(
            "INSERT INTO gateway_settings (id, audit_mode, unicode_detection, tool_cmd_detection,"
            " outbound_detection, retry_count, disabled_rules, blacklist, whitelist,"
            " log_retention_days, default_model, internal_key, outbound_keys,"
            " security_alert_min_severity, updated_at)"
            " VALUES ('default', 'audit', 1, 1, 1, 2, '[]', '[]', '[]', 30, '', '', '{}',"
            " 'medium', ?)",
            (_now(),),
        )
        row = db.query_one("SELECT * FROM gateway_settings WHERE id = 'default'")
    out = dict(_DEFAULTS)
    if row:
        for k in _DEFAULTS:
            if k in _JSON_FIELDS:
                out[k] = _parse_json(row.get(k), _DEFAULTS[k])
            elif k in _DICT_FIELDS:
                val = _parse_json_obj(row.get(k), {})
                out[k] = val if isinstance(val, dict) else {}
            elif k in row.keys():
                out[k] = row[k]
    # 归一化审计模式
    if out.get("audit_mode") not in security.AUDIT_MODES:
        out["audit_mode"] = "audit"
    if out.get("security_alert_min_severity") not in _ALERT_SEVERITIES:
        out["security_alert_min_severity"] = "medium"
    return out


def save_settings(patch: dict) -> dict:
    """增量保存配置（只更新传入字段）。"""
    cur = get_settings()
    mode = patch.get("audit_mode")
    if mode is not None:
        cur["audit_mode"] = mode if mode in security.AUDIT_MODES else cur["audit_mode"]
    for k in ("unicode_detection", "tool_cmd_detection", "outbound_detection"):
        if k in patch:
            cur[k] = 1 if patch[k] else 0
    if "retry_count" in patch:
        try:
            cur["retry_count"] = max(0, min(5, int(patch["retry_count"])))
        except (TypeError, ValueError):
            pass
    if "log_retention_days" in patch:
        try:
            cur["log_retention_days"] = max(1, min(365, int(patch["log_retention_days"])))
        except (TypeError, ValueError):
            pass
    for k in _JSON_FIELDS:
        if k in patch and isinstance(patch[k], list):
            cur[k] = [str(x).strip() for x in patch[k] if str(x).strip()]
    if "default_model" in patch:
        cur["default_model"] = str(patch["default_model"] or "").strip()
    if "internal_key" in patch:
        cur["internal_key"] = str(patch["internal_key"] or "").strip()
    if "security_alert_min_severity" in patch:
        sev = str(patch["security_alert_min_severity"] or "").strip().lower()
        if sev in _ALERT_SEVERITIES:
            cur["security_alert_min_severity"] = sev

    db.execute(
        "UPDATE gateway_settings SET audit_mode=?, unicode_detection=?, tool_cmd_detection=?,"
        " outbound_detection=?, retry_count=?, disabled_rules=?, blacklist=?, whitelist=?,"
        " log_retention_days=?, default_model=?, internal_key=?,"
        " security_alert_min_severity=?, updated_at=? WHERE id='default'",
        (
            cur["audit_mode"], cur["unicode_detection"], cur["tool_cmd_detection"],
            cur["outbound_detection"], cur["retry_count"],
            json.dumps(cur["disabled_rules"], ensure_ascii=False),
            json.dumps(cur["blacklist"], ensure_ascii=False),
            json.dumps(cur["whitelist"], ensure_ascii=False),
            cur["log_retention_days"], cur["default_model"], cur["internal_key"],
            cur["security_alert_min_severity"], _now(),
        ),
    )
    return get_settings()


# ---------------------------------------------------------------------------
# 出网密钥明文映射（公共密钥/用户密钥由系统按用户解析后自动携带）
# ---------------------------------------------------------------------------

def get_outbound_keys() -> dict:
    """{key_id: 明文} 映射（本地信任边界，管理接口不回传明文）。"""
    val = get_settings().get("outbound_keys") or {}
    return val if isinstance(val, dict) else {}


def set_outbound_key(key_id: str, plain: str) -> None:
    """登记/更新某密钥的出网明文（plain 为空表示移除映射）。"""
    mapping = get_outbound_keys()
    if plain:
        mapping[key_id] = plain
    else:
        mapping.pop(key_id, None)
    db.execute("UPDATE gateway_settings SET outbound_keys=?, updated_at=? WHERE id='default'",
               (json.dumps(mapping, ensure_ascii=False), _now()))


def remove_outbound_keys(key_ids) -> None:
    """批量移除映射（密钥删除/禁用时清理）。"""
    mapping = get_outbound_keys()
    changed = False
    for kid in key_ids or []:
        if mapping.pop(str(kid), None) is not None:
            changed = True
    if changed:
        db.execute("UPDATE gateway_settings SET outbound_keys=?, updated_at=? WHERE id='default'",
                   (json.dumps(mapping, ensure_ascii=False), _now()))
