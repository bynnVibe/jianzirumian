"""
见字如面 - 模型网关「上游管理」（璇玑式）

上游 = 一个可出网的模型服务端点，自身持有：协议 / Base URL / API Key / 成本层级 /
模型清单 / 权重 / 健康度 / 延迟。路由规则（rules.py）按模型模式选中一组上游并做负载均衡；
本模块只负责上游实体的 CRUD、生效出网配置解析、连通性诊断与健康/延迟维护。

- tier=paid|free|subscription；tier=free 且未填 Base URL/Key 时回退系统 OpenRouter 配置。
- 模型清单（models）为该上游可服务的模型名列表；请求命中规则后按原名出网（无模型映射）。
- 健康度：连续失败 >=3 次熔断为 down，1~2 次 degraded，成功一次恢复 healthy 并 EMA 更新延迟。
"""
import json
import logging
import time
import uuid
from datetime import datetime
from typing import List, Optional

import httpx

from app.core import db

logger = logging.getLogger("jianziruyang.gateway.upstreams")

VALID_PROTOCOLS = ("openai", "anthropic", "ollama")
TIERS = ("paid", "free", "subscription")
# 连续失败达到该阈值即熔断为 down
_DOWN_THRESHOLD = 3
# 延迟 EMA 平滑系数（新样本权重）
_LATENCY_ALPHA = 0.3
_TEST_TIMEOUT = httpx.Timeout(connect=8, read=30, write=15, pool=5)


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


def _mask_secret(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * 6}{secret[-4:]}"


def _normalize_models(value) -> List[str]:
    """模型清单归一化：支持列表或逗号分隔字符串（图2「模型列表」输入），去重去空。"""
    if isinstance(value, str):
        value = value.replace("，", ",").split(",")
    if not isinstance(value, list):
        return []
    seen: List[str] = []
    for m in value:
        s = str(m).strip()
        if s and s not in seen:
            seen.append(s)
    return seen


def _normalize_tier(value) -> str:
    s = (value or "").strip().lower()
    return s if s in TIERS else "paid"


def _system_openrouter_conn() -> dict:
    """系统 OpenRouter 配置（系统设置 custom provider 指向 openrouter.ai）；未配置返回 {}。"""
    from app.services.config_manager import effective_provider_config
    try:
        cfg = effective_provider_config("llm", "custom") or {}
    except Exception as e:
        logger.warning("读取系统 OpenRouter 配置失败: %s", e)
        return {}
    if "openrouter.ai" not in (cfg.get("base_url") or ""):
        return {}
    return {"base_url": (cfg.get("base_url") or "").rstrip("/"),
            "api_key": (cfg.get("api_key") or "").strip()}


# ---------------------------------------------------------------------------
# 生效出网配置解析 + URL 拼装
# ---------------------------------------------------------------------------

def effective(up: dict) -> dict:
    """上游生效出网配置（proxy 出网与测试连接的唯一解析出口）。

    tier=free 且未填 Base URL/Key 时回退系统 OpenRouter 配置（Base URL 再兜底官方地址）。
    返回 {protocol, base_url, api_key, name}，供协议适配层 build_request/build_url 使用。
    """
    from app.core import openrouter as or_svc
    protocol = (up.get("protocol") or "openai").strip().lower()
    base_url = (up.get("base_url") or "").strip().rstrip("/")
    api_key = (up.get("api_key") or "").strip()
    tier = (up.get("tier") or "paid").strip().lower()
    if tier == "free" and (not base_url or not api_key):
        sys_conn = _system_openrouter_conn()
        if not base_url:
            base_url = (sys_conn.get("base_url") or or_svc.OPENROUTER_BASE_URL).rstrip("/")
        if not api_key:
            api_key = sys_conn.get("api_key") or ""
    return {"protocol": protocol, "base_url": base_url, "api_key": api_key,
            "name": up.get("name") or ""}


def build_upstream_url(eff: dict) -> str:
    """按协议拼装上游请求 URL。"""
    base = (eff.get("base_url") or "").rstrip("/")
    protocol = eff.get("protocol") or "openai"
    if protocol == "ollama":
        return f"{base}/api/chat"
    if protocol == "anthropic":
        return f"{base}/v1/messages" if not base.endswith("/messages") else base
    # openai 兼容：允许 base_url 已含 /v1 或 /chat/completions
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def _row_to_dict(row: dict, with_secret: bool = False) -> dict:
    models = _parse_json(row.get("models"), [])
    out = {
        "id": row["id"],
        "name": row["name"],
        "protocol": row.get("protocol") or "openai",
        "base_url": row.get("base_url") or "",
        "tier": row.get("tier") or "paid",
        "models": models,
        "model_count": len(models),
        "weight": int(row.get("weight") or 100),
        "enabled": bool(row.get("enabled")),
        "health": row.get("health") or "healthy",
        "fail_count": int(row.get("fail_count") or 0),
        "success_count": int(row.get("success_count") or 0),
        "last_error": row.get("last_error") or "",
        "latency_ms": int(row.get("latency_ms") or 0),
        "remark": row.get("remark") or "",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        # api_key 脱敏展示（仅保留尾 4 位），避免前端泄露上游密钥
        "api_key_masked": _mask_secret(row.get("api_key") or ""),
        "has_api_key": bool(row.get("api_key")),
    }
    if with_secret:
        out["api_key"] = row.get("api_key") or ""
    return out


def list_upstreams() -> List[dict]:
    rows = db.query("SELECT * FROM gateway_upstreams ORDER BY created_at ASC")
    return [_row_to_dict(r) for r in rows]


def get_upstream(upstream_id: str, with_secret: bool = False) -> Optional[dict]:
    row = db.query_one("SELECT * FROM gateway_upstreams WHERE id=?", (upstream_id,))
    return _row_to_dict(row, with_secret) if row else None


def create_upstream(payload: dict) -> dict:
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("上游名称不能为空")
    protocol = (payload.get("protocol") or "openai").strip().lower()
    if protocol not in VALID_PROTOCOLS:
        raise ValueError(f"不支持的协议：{protocol}（可选 {'/'.join(VALID_PROTOCOLS)}）")
    tier = _normalize_tier(payload.get("tier"))
    base_url = (payload.get("base_url") or "").strip().rstrip("/")
    # free 层级允许空 Base URL（回退系统 OpenRouter）；付费/订阅需显式填写
    if tier != "free" and not base_url:
        raise ValueError("付费/订阅上游需填写 Base URL")
    models = _normalize_models(payload.get("models"))
    uid = _new_id()
    db.execute(
        "INSERT INTO gateway_upstreams (id, name, protocol, base_url, api_key, remark,"
        " tier, models, weight, enabled, health, fail_count, success_count, last_error,"
        " latency_ms, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,'healthy',0,0,'',0,?,?)",
        (uid, name, protocol, base_url, (payload.get("api_key") or "").strip(),
         (payload.get("remark") or "").strip(), tier,
         json.dumps(models, ensure_ascii=False), max(1, int(payload.get("weight") or 100)),
         1 if payload.get("enabled", True) else 0, _now(), _now()),
    )
    return get_upstream(uid)


def update_upstream(upstream_id: str, patch: dict) -> Optional[dict]:
    cur = db.query_one("SELECT * FROM gateway_upstreams WHERE id=?", (upstream_id,))
    if not cur:
        return None
    name = (patch.get("name") or cur["name"]).strip()
    protocol = (patch.get("protocol") or cur["protocol"]).strip().lower()
    if protocol not in VALID_PROTOCOLS:
        raise ValueError(f"不支持的协议：{protocol}（可选 {'/'.join(VALID_PROTOCOLS)}）")
    tier = _normalize_tier(patch.get("tier") if "tier" in patch else cur.get("tier"))
    base_url = (patch.get("base_url", cur["base_url"]) or "").strip().rstrip("/")
    if tier != "free" and not base_url:
        raise ValueError("付费/订阅上游需填写 Base URL")
    # api_key 为空字符串表示不修改（前端脱敏展示不回传明文）
    api_key = cur["api_key"]
    if "api_key" in patch and str(patch.get("api_key") or "").strip():
        api_key = str(patch["api_key"]).strip()
    models = _normalize_models(patch["models"]) if "models" in patch \
        else _parse_json(cur["models"], [])
    weight = max(1, int(patch.get("weight", cur["weight"]) or 100))
    enabled = 1 if patch.get("enabled", bool(cur["enabled"])) else 0
    remark = (patch.get("remark", cur["remark"]) or "").strip()
    db.execute(
        "UPDATE gateway_upstreams SET name=?, protocol=?, base_url=?, api_key=?, remark=?,"
        " tier=?, models=?, weight=?, enabled=?, updated_at=? WHERE id=?",
        (name, protocol, base_url, api_key, remark, tier,
         json.dumps(models, ensure_ascii=False), weight, enabled, _now(), upstream_id),
    )
    return get_upstream(upstream_id)


def clone_upstream(upstream_id: str) -> Optional[dict]:
    """克隆上游（图1「克隆」操作）：复制连接配置与模型清单，健康度重置，名称加「副本」。"""
    src = db.query_one("SELECT * FROM gateway_upstreams WHERE id=?", (upstream_id,))
    if not src:
        return None
    uid = _new_id()
    db.execute(
        "INSERT INTO gateway_upstreams (id, name, protocol, base_url, api_key, remark,"
        " tier, models, weight, enabled, health, fail_count, success_count, last_error,"
        " latency_ms, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,'healthy',0,0,'',0,?,?)",
        (uid, f"{src['name']} 副本", src.get("protocol") or "openai",
         src.get("base_url") or "", src.get("api_key") or "", src.get("remark") or "",
         src.get("tier") or "paid", src.get("models") or "[]",
         int(src.get("weight") or 100), 1, _now(), _now()),
    )
    return get_upstream(uid)


def delete_upstream(upstream_id: str) -> bool:
    ok = db.execute("DELETE FROM gateway_upstreams WHERE id=?", (upstream_id,)) > 0
    if ok:
        # 从所有路由规则的 upstreams 列表中剔除该上游，避免悬空引用
        for r in db.query("SELECT id, upstreams FROM gateway_rules"):
            ups = _parse_json(r.get("upstreams"), [])
            if upstream_id in ups:
                kept = [u for u in ups if u != upstream_id]
                db.execute("UPDATE gateway_rules SET upstreams=?, updated_at=? WHERE id=?",
                           (json.dumps(kept, ensure_ascii=False), _now(), r["id"]))
    return ok


# ---------------------------------------------------------------------------
# 健康度 + 延迟
# ---------------------------------------------------------------------------

def mark_success(upstream_id: str, latency_ms: int = 0) -> None:
    row = db.query_one("SELECT latency_ms FROM gateway_upstreams WHERE id=?", (upstream_id,))
    if not row:
        return
    prev = int(row.get("latency_ms") or 0)
    lat = prev
    if latency_ms and latency_ms > 0:
        lat = int(prev * (1 - _LATENCY_ALPHA) + latency_ms * _LATENCY_ALPHA) if prev \
            else int(latency_ms)
    db.execute(
        "UPDATE gateway_upstreams SET fail_count=0, health='healthy',"
        " success_count=success_count+1, last_error='', latency_ms=?, updated_at=? WHERE id=?",
        (lat, _now(), upstream_id),
    )


def mark_failure(upstream_id: str, error: str = "") -> None:
    row = db.query_one("SELECT fail_count FROM gateway_upstreams WHERE id=?", (upstream_id,))
    if not row:
        return
    fails = int(row.get("fail_count") or 0) + 1
    health = "down" if fails >= _DOWN_THRESHOLD else "degraded"
    db.execute(
        "UPDATE gateway_upstreams SET fail_count=?, health=?, last_error=?, updated_at=?"
        " WHERE id=?",
        (fails, health, (error or "")[:300], _now(), upstream_id),
    )
    if health == "down":
        logger.warning("上游 %s 连续失败 %d 次，已熔断(down)", upstream_id, fails)


def reset_health(upstream_id: str) -> None:
    db.execute(
        "UPDATE gateway_upstreams SET fail_count=0, health='healthy', last_error='', updated_at=?"
        " WHERE id=?", (_now(), upstream_id),
    )


# ---------------------------------------------------------------------------
# 聚合模型清单 + OpenRouter 免费模型
# ---------------------------------------------------------------------------

def all_model_names() -> List[str]:
    """汇总所有启用上游的模型清单（供 /v1/models 与前端下拉/规则模型提示）。"""
    rows = db.query("SELECT models FROM gateway_upstreams WHERE enabled=1")
    seen = set()
    for r in rows:
        for m in _parse_json(r.get("models"), []):
            if str(m).strip():
                seen.add(str(m).strip())
    return sorted(seen)


async def fetch_openrouter_free() -> List[str]:
    """实时拉取 OpenRouter 免费模型 id 列表（失败返回空列表）。"""
    from app.core import openrouter
    return await openrouter.fetch_free_model_ids()


# ---------------------------------------------------------------------------
# 连通性诊断：对生效配置真实发一次最小 chat 请求
# ---------------------------------------------------------------------------

async def _probe(eff: dict, model: str) -> dict:
    """对生效配置发一次最小 chat 请求，返回单模型探测结果（不更新健康度）。

    401/403 归因鉴权、404 归因模型不存在/已下线、429 归因限流，其余非 2xx 附上游响应预览。
    """
    url = build_upstream_url(eff)
    body = {"model": model, "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8, "temperature": 0, "stream": False}
    t0 = time.monotonic()
    try:
        from app.services.gateway import upstream as transport
        headers, payload = transport.build_request(eff, body, model, stream=False)
        async with httpx.AsyncClient(timeout=_TEST_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as e:
        return {"model": model, "success": False,
                "message": f"连接失败：{e}（请检查 Base URL 是否可达、协议是否匹配）"}

    sc = resp.status_code
    latency = int((time.monotonic() - t0) * 1000)
    preview = (resp.text or "")[:200]
    if sc < 400:
        note = "" if eff["api_key"] else "（未配置上游 API Key，该上游未强制鉴权）"
        return {"model": model, "success": True, "status_code": sc, "latency_ms": latency,
                "preview": preview,
                "message": f"上游返回 HTTP {sc}，模型 {model} 可正常应答，延迟 {latency}ms{note}"}
    if sc in (401, 403):
        key_hint = "（当前未配置上游 API Key，请在编辑中填写）" if not eff["api_key"] \
            else "（请检查上游 API Key 是否有效）"
        msg = f"鉴权失败：上游返回 HTTP {sc}{key_hint}"
    elif sc == 404:
        msg = f"模型不可用：上游返回 HTTP 404，{model} 不存在或已下线"
    elif sc == 429:
        msg = (f"上游限流 HTTP 429：配置完整且鉴权通过，但 {model} 当前被限流，"
               "稍后重试或更换模型")
    else:
        msg = f"调用失败：上游返回 HTTP {sc}"
    return {"model": model, "success": False, "status_code": sc, "message": msg,
            "preview": preview}


async def test_connection(upstream_id: str, model: str = "") -> dict:
    """逐模型探测上游生效配置（验证配置完整性/连通性/可调用性，可能产生上游费用）。

    缺省对模型清单中每个模型各发一次最小 chat 请求（指定 model 时仅测该模型），
    任一模型成功即视为上游可达并更新健康度与延迟，全部失败记一次熔断计数。
    返回 {"success", "message", "results": [逐模型结果], ...}；不抛异常。
    """
    up = get_upstream(upstream_id, with_secret=True)
    if not up:
        raise ValueError("上游不存在")
    eff = effective(up)
    target = (model or "").strip()
    targets = [target] if target else list(up.get("models") or [])
    missing = []
    if not eff["base_url"]:
        missing.append("Base URL")
    if not targets:
        missing.append("模型（请在模型列表中至少填写一个）")
    if missing:
        return {"success": False, "results": [],
                "message": f"配置不全：缺少 {'、'.join(missing)}，请在编辑中补全后再测试"}

    results = [await _probe(eff, m) for m in targets]
    ok = [r for r in results if r["success"]]
    if ok:
        lat = [r.get("latency_ms") or 0 for r in ok]
        mark_success(upstream_id, int(sum(lat) / len(lat)))
    else:
        mark_failure(upstream_id, results[0].get("message") or "测试失败")
    all_ok = len(ok) == len(results)
    message = (f"共测试 {len(results)} 个模型，全部成功" if all_ok
               else f"共测试 {len(results)} 个模型：{len(ok)} 个成功、"
                    f"{len(results) - len(ok)} 个失败")
    return {"success": all_ok, "ok_count": len(ok), "total": len(results),
            "results": results, "message": message}
