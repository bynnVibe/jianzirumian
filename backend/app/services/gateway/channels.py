"""
见字如面 - 模型网关上游渠道管理 + 负载均衡 + 故障切换

渠道 = 一个上游 LLM 供应商端点（协议 + BaseUrl + ApiKey + 模型清单/映射 + 优先级 + 权重）。
选路策略：按 priority 从高到低分层，同层内按 weight 做加权随机（A-Res 算法），返回
候选渠道有序列表；代理按序尝试，某渠道失败则自动切换到下一个（故障切换），并更新健康度。

健康度：连续失败 >=3 次熔断为 down（暂时跳过），1~2 次为 degraded，成功一次恢复 healthy。
down 的渠道仍作为最后兜底候选（当所有渠道都 down 时不至于直接拒绝）。
"""
import json
import logging
import math
import random
import uuid
from datetime import datetime
from typing import List, Optional

from app.core import db

logger = logging.getLogger("jianziruyang.gateway.channels")

VALID_PROTOCOLS = ("openai", "anthropic", "ollama")
# 连续失败达到该阈值即熔断为 down
_DOWN_THRESHOLD = 3


def _now() -> str:
    return datetime.now().isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _parse_json(raw, fallback):
    try:
        val = json.loads(raw or "")
        return val if isinstance(val, (list, dict)) else fallback
    except (json.JSONDecodeError, TypeError):
        return fallback


def _row_to_dict(row: dict, with_secret: bool = False) -> dict:
    out = {
        "id": row["id"],
        "name": row["name"],
        "protocol": row.get("protocol") or "openai",
        "base_url": row.get("base_url") or "",
        "models": _parse_json(row.get("models"), []),
        "model_mapping": _parse_json(row.get("model_mapping"), {}),
        "priority": int(row.get("priority") or 0),
        "weight": int(row.get("weight") or 1),
        "enabled": bool(row.get("enabled")),
        "health": row.get("health") or "healthy",
        "fail_count": int(row.get("fail_count") or 0),
        "success_count": int(row.get("success_count") or 0),
        "last_error": row.get("last_error") or "",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        # api_key 脱敏展示（仅保留尾 4 位），避免前端泄露上游密钥
        "api_key_masked": _mask_secret(row.get("api_key") or ""),
        "has_api_key": bool(row.get("api_key")),
    }
    if with_secret:
        out["api_key"] = row.get("api_key") or ""
    return out


def _mask_secret(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * 6}{secret[-4:]}"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_channels() -> List[dict]:
    rows = db.query("SELECT * FROM gateway_channels ORDER BY priority DESC, created_at DESC")
    return [_row_to_dict(r) for r in rows]


def get_channel(channel_id: str, with_secret: bool = False) -> Optional[dict]:
    row = db.query_one("SELECT * FROM gateway_channels WHERE id=?", (channel_id,))
    return _row_to_dict(row, with_secret) if row else None


def create_channel(payload: dict) -> dict:
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("渠道名称不能为空")
    protocol = (payload.get("protocol") or "openai").strip().lower()
    if protocol not in VALID_PROTOCOLS:
        raise ValueError(f"不支持的协议：{protocol}（可选 {'/'.join(VALID_PROTOCOLS)}）")
    base_url = (payload.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("BaseUrl 不能为空")
    channel_id = _new_id()
    models = [str(m).strip() for m in (payload.get("models") or []) if str(m).strip()]
    mapping = payload.get("model_mapping") or {}
    if not isinstance(mapping, dict):
        mapping = {}
    db.execute(
        "INSERT INTO gateway_channels (id, name, protocol, base_url, api_key, models,"
        " model_mapping, priority, weight, enabled, health, fail_count, success_count,"
        " last_error, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,'healthy',0,0,'',?,?)",
        (channel_id, name, protocol, base_url, (payload.get("api_key") or "").strip(),
         json.dumps(models, ensure_ascii=False), json.dumps(mapping, ensure_ascii=False),
         int(payload.get("priority") or 0), max(1, int(payload.get("weight") or 1)),
         1 if payload.get("enabled", True) else 0, _now(), _now()),
    )
    return get_channel(channel_id)


def update_channel(channel_id: str, patch: dict) -> Optional[dict]:
    cur = db.query_one("SELECT * FROM gateway_channels WHERE id=?", (channel_id,))
    if not cur:
        return None
    name = (patch.get("name") or cur["name"]).strip()
    protocol = (patch.get("protocol") or cur["protocol"]).strip().lower()
    base_url = (patch.get("base_url", cur["base_url"]) or "").strip().rstrip("/")
    # api_key 为空字符串表示不修改（前端脱敏展示不回传明文）
    api_key = cur["api_key"]
    if "api_key" in patch and str(patch.get("api_key") or "").strip():
        api_key = str(patch["api_key"]).strip()
    models = patch.get("models")
    models = [str(m).strip() for m in models if str(m).strip()] if isinstance(models, list) \
        else _parse_json(cur["models"], [])
    mapping = patch.get("model_mapping")
    mapping = mapping if isinstance(mapping, dict) else _parse_json(cur["model_mapping"], {})
    priority = int(patch.get("priority", cur["priority"]) or 0)
    weight = max(1, int(patch.get("weight", cur["weight"]) or 1))
    enabled = 1 if patch.get("enabled", bool(cur["enabled"])) else 0
    db.execute(
        "UPDATE gateway_channels SET name=?, protocol=?, base_url=?, api_key=?, models=?,"
        " model_mapping=?, priority=?, weight=?, enabled=?, updated_at=? WHERE id=?",
        (name, protocol, base_url, api_key, json.dumps(models, ensure_ascii=False),
         json.dumps(mapping, ensure_ascii=False), priority, weight, enabled, _now(), channel_id),
    )
    return get_channel(channel_id)


def delete_channel(channel_id: str) -> bool:
    return db.execute("DELETE FROM gateway_channels WHERE id=?", (channel_id,)) > 0


# ---------------------------------------------------------------------------
# 负载均衡 + 故障切换
# ---------------------------------------------------------------------------

def _channel_serves_model(row: dict, model: str) -> bool:
    """渠道是否服务某模型：models 为空视为通配；否则命中清单或映射键。"""
    models = _parse_json(row.get("models"), [])
    if not models:
        return True
    if model in models:
        return True
    mapping = _parse_json(row.get("model_mapping"), {})
    return model in mapping


def _weighted_order(rows: List[dict]) -> List[dict]:
    """同优先级层内按权重加权随机排序（A-Res：key = rand^(1/weight)，取降序）。"""
    keyed = []
    for r in rows:
        w = max(1, int(r.get("weight") or 1))
        rnd = random.random() or 1e-9
        keyed.append((-math.log(rnd) / w, r))
    keyed.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in keyed]


def select_candidates(model: str) -> List[dict]:
    """返回服务该模型的候选渠道（含密钥），按 优先级分层 + 层内加权随机 排序。

    健康渠道排在熔断渠道之前（同优先级内），保证故障切换优先尝试健康渠道。
    """
    rows = db.query("SELECT * FROM gateway_channels WHERE enabled=1")
    serving = [r for r in rows if _channel_serves_model(r, model)]
    if not serving:
        return []
    # 按优先级分层
    tiers: dict[int, List[dict]] = {}
    for r in serving:
        tiers.setdefault(int(r.get("priority") or 0), []).append(r)
    ordered: List[dict] = []
    for prio in sorted(tiers.keys(), reverse=True):
        tier = tiers[prio]
        healthy = [r for r in tier if (r.get("health") or "healthy") != "down"]
        down = [r for r in tier if (r.get("health") or "healthy") == "down"]
        ordered.extend(_weighted_order(healthy))
        ordered.extend(_weighted_order(down))  # 熔断渠道兜底
    return ordered


def map_model(channel_row: dict, requested_model: str) -> str:
    """把请求模型名映射为上游真实模型名（无映射则原样）。"""
    mapping = _parse_json(channel_row.get("model_mapping"), {})
    return mapping.get(requested_model, requested_model)


def build_upstream_url(channel_row: dict) -> str:
    """按协议拼装上游请求 URL。"""
    base = (channel_row.get("base_url") or "").rstrip("/")
    protocol = channel_row.get("protocol") or "openai"
    if protocol == "ollama":
        return f"{base}/api/chat"
    if protocol == "anthropic":
        return f"{base}/v1/messages" if not base.endswith("/messages") else base
    # openai 兼容：允许 base_url 已含 /v1
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def mark_success(channel_id: str) -> None:
    db.execute(
        "UPDATE gateway_channels SET fail_count=0, health='healthy',"
        " success_count=success_count+1, last_error='', updated_at=? WHERE id=?",
        (_now(), channel_id),
    )


def mark_failure(channel_id: str, error: str = "") -> None:
    row = db.query_one("SELECT fail_count FROM gateway_channels WHERE id=?", (channel_id,))
    if not row:
        return
    fails = int(row.get("fail_count") or 0) + 1
    health = "down" if fails >= _DOWN_THRESHOLD else "degraded"
    db.execute(
        "UPDATE gateway_channels SET fail_count=?, health=?, last_error=?, updated_at=? WHERE id=?",
        (fails, health, (error or "")[:300], _now(), channel_id),
    )
    if health == "down":
        logger.warning("渠道 %s 连续失败 %d 次，已熔断(down)", channel_id, fails)


def reset_health(channel_id: str) -> None:
    db.execute(
        "UPDATE gateway_channels SET fail_count=0, health='healthy', last_error='', updated_at=?"
        " WHERE id=?", (_now(), channel_id),
    )


def all_models() -> List[str]:
    """汇总所有启用渠道对外暴露的模型名（含映射键），供 /v1/models 与前端下拉。"""
    rows = db.query("SELECT models, model_mapping FROM gateway_channels WHERE enabled=1")
    seen = set()
    for r in rows:
        for m in _parse_json(r.get("models"), []):
            seen.add(m)
        for k in _parse_json(r.get("model_mapping"), {}).keys():
            seen.add(k)
    return sorted(seen)
