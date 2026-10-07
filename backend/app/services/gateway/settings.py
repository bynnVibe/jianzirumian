"""
见字如面 - 模型网关全局配置

单行配置（id='default'）：审计模式、三类检测开关、失败重试次数、内置规则停用清单、
自定义黑白名单、日志保留天数。读取时对缺失字段回退默认值，保证向前兼容。
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
}

_JSON_FIELDS = ("disabled_rules", "blacklist", "whitelist")


def _now() -> str:
    return datetime.now().isoformat()


def _parse_json(raw, fallback):
    try:
        val = json.loads(raw or "[]")
        return val if isinstance(val, list) else fallback
    except (json.JSONDecodeError, TypeError):
        return fallback


def get_settings() -> dict:
    """读取网关配置（缺失时惰性初始化默认行）。"""
    row = db.query_one("SELECT * FROM gateway_settings WHERE id = 'default'")
    if not row:
        db.execute(
            "INSERT INTO gateway_settings (id, audit_mode, unicode_detection, tool_cmd_detection,"
            " outbound_detection, retry_count, disabled_rules, blacklist, whitelist,"
            " log_retention_days, updated_at)"
            " VALUES ('default', 'audit', 1, 1, 1, 2, '[]', '[]', '[]', 30, ?)",
            (_now(),),
        )
        row = db.query_one("SELECT * FROM gateway_settings WHERE id = 'default'")
    out = dict(_DEFAULTS)
    if row:
        for k in _DEFAULTS:
            if k in _JSON_FIELDS:
                out[k] = _parse_json(row.get(k), _DEFAULTS[k])
            elif k in row.keys():
                out[k] = row[k]
    # 归一化审计模式
    if out.get("audit_mode") not in security.AUDIT_MODES:
        out["audit_mode"] = "audit"
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

    db.execute(
        "UPDATE gateway_settings SET audit_mode=?, unicode_detection=?, tool_cmd_detection=?,"
        " outbound_detection=?, retry_count=?, disabled_rules=?, blacklist=?, whitelist=?,"
        " log_retention_days=?, updated_at=? WHERE id='default'",
        (
            cur["audit_mode"], cur["unicode_detection"], cur["tool_cmd_detection"],
            cur["outbound_detection"], cur["retry_count"],
            json.dumps(cur["disabled_rules"], ensure_ascii=False),
            json.dumps(cur["blacklist"], ensure_ascii=False),
            json.dumps(cur["whitelist"], ensure_ascii=False),
            cur["log_retention_days"], _now(),
        ),
    )
    return get_settings()
