"""
见字如面 - 模型网关管理 API（管理员 / 运维角色）

覆盖四大模块的后台管理与可视化数据：
- 密钥与配额：/api/gateway/keys（增删改查 + 重置用量）
- 渠道与负载：/api/gateway/channels（增删改查 + 连通性测试 + 健康重置）
- 日志与审计：/api/gateway/logs（搜索/筛选/分页 + 展开明细含链路）
- 安全审计中心：/api/gateway/settings、/api/gateway/rules
- 仪表盘：/api/gateway/overview、/api/gateway/stats

所有端点均通过 require_ops 校验（admin 与 ops 均可访问）。
"""
import json
import logging
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from app.api.deps import require_ops
from app.services.gateway import channels as ch_svc
from app.services.gateway import keys as key_svc
from app.services.gateway import logs as log_svc
from app.services.gateway import security
from app.services.gateway import settings as settings_svc
from app.services.gateway import upstream

logger = logging.getLogger("jianziruyang.gateway.api")

router = APIRouter(prefix="/api/gateway", tags=["gateway"])

_TEST_TIMEOUT = httpx.Timeout(connect=8, read=30, write=15, pool=5)


# ---------------------------------------------------------------------------
# 请求体模型
# ---------------------------------------------------------------------------

class ChannelBody(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    name: str
    protocol: str = "openai"
    base_url: str
    api_key: str = ""
    models: List[str] = []
    model_mapping: dict = {}
    priority: int = 0
    weight: int = 1
    enabled: bool = True


class ChannelPatch(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    name: Optional[str] = None
    protocol: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    models: Optional[List[str]] = None
    model_mapping: Optional[dict] = None
    priority: Optional[int] = None
    weight: Optional[int] = None
    enabled: Optional[bool] = None


class KeyBody(BaseModel):
    name: str
    token_quota: int = 0
    request_quota: int = 0
    allowed_models: List[str] = []
    expires_at: Optional[str] = None


class KeyPatch(BaseModel):
    name: Optional[str] = None
    token_quota: Optional[int] = None
    request_quota: Optional[int] = None
    allowed_models: Optional[List[str]] = None
    enabled: Optional[bool] = None
    expires_at: Optional[str] = None


class SettingsBody(BaseModel):
    audit_mode: Optional[str] = None
    unicode_detection: Optional[bool] = None
    tool_cmd_detection: Optional[bool] = None
    outbound_detection: Optional[bool] = None
    retry_count: Optional[int] = None
    disabled_rules: Optional[List[str]] = None
    blacklist: Optional[List[str]] = None
    whitelist: Optional[List[str]] = None
    log_retention_days: Optional[int] = None


# ---------------------------------------------------------------------------
# 仪表盘
# ---------------------------------------------------------------------------

@router.get("/overview")
async def overview(_ops: dict = Depends(require_ops)):
    """网关总览：渠道/密钥计数 + 近 7 天调用统计 + 最近风险日志。"""
    stats = log_svc.stats(7)
    channels = ch_svc.list_channels()
    keys = key_svc.list_keys()
    risky = log_svc.query_logs(risk_level="high", limit=5)
    risky_crit = log_svc.query_logs(risk_level="critical", limit=5)
    health = {"healthy": 0, "degraded": 0, "down": 0}
    for c in channels:
        health[c.get("health", "healthy")] = health.get(c.get("health", "healthy"), 0) + 1
    return {
        "success": True,
        "channel_count": len(channels),
        "channel_enabled": sum(1 for c in channels if c.get("enabled")),
        "channel_health": health,
        "key_count": len(keys),
        "key_enabled": sum(1 for k in keys if k.get("enabled")),
        "stats": stats,
        "recent_risks": (risky_crit["items"] + risky["items"])[:8],
    }


@router.get("/stats")
async def stats(days: int = 7, _ops: dict = Depends(require_ops)):
    days = max(1, min(days, 90))
    return {"success": True, **log_svc.stats(days)}


# ---------------------------------------------------------------------------
# 渠道管理（负载均衡 + 故障切换）
# ---------------------------------------------------------------------------

@router.get("/channels")
async def list_channels(_ops: dict = Depends(require_ops)):
    return {"success": True, "channels": ch_svc.list_channels(),
            "models": ch_svc.all_models()}


@router.post("/channels")
async def create_channel(body: ChannelBody, _ops: dict = Depends(require_ops)):
    try:
        ch = ch_svc.create_channel(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "channel": ch}


@router.put("/channels/{channel_id}")
async def update_channel(channel_id: str, body: ChannelPatch, _ops: dict = Depends(require_ops)):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        ch = ch_svc.update_channel(channel_id, patch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ch:
        raise HTTPException(status_code=404, detail="渠道不存在")
    return {"success": True, "channel": ch}


@router.delete("/channels/{channel_id}")
async def delete_channel(channel_id: str, _ops: dict = Depends(require_ops)):
    if not ch_svc.delete_channel(channel_id):
        raise HTTPException(status_code=404, detail="渠道不存在")
    return {"success": True}


@router.post("/channels/{channel_id}/reset-health")
async def reset_channel_health(channel_id: str, _ops: dict = Depends(require_ops)):
    ch_svc.reset_health(channel_id)
    return {"success": True, "channel": ch_svc.get_channel(channel_id)}


@router.post("/channels/{channel_id}/test")
async def test_channel(channel_id: str, _ops: dict = Depends(require_ops)):
    """向渠道发一条最小补全请求，验证连通性与协议配置（不计入网关日志/配额）。"""
    row = ch_svc.get_channel(channel_id, with_secret=True)
    if not row:
        raise HTTPException(status_code=404, detail="渠道不存在")
    models = row.get("models") or []
    mapping = row.get("model_mapping") or {}
    model = (mapping and next(iter(mapping.keys()))) or (models[0] if models else "gpt-3.5-turbo")
    mapped = ch_svc.map_model(row, model)
    url = ch_svc.build_upstream_url(row)
    body = {"model": model, "messages": [{"role": "user", "content": "ping"}],
            "stream": False, "max_tokens": 5}
    try:
        headers, payload = upstream.build_request(row, body, mapped, stream=False)
        async with httpx.AsyncClient(timeout=_TEST_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=payload)
        ok = resp.status_code < 400
        if ok:
            ch_svc.mark_success(channel_id)
        else:
            ch_svc.mark_failure(channel_id, f"测试 HTTP {resp.status_code}")
        return {"success": ok, "status_code": resp.status_code, "url": url,
                "model": mapped, "detail": resp.text[:300]}
    except Exception as e:
        ch_svc.mark_failure(channel_id, f"测试失败: {e}")
        return {"success": False, "status_code": 0, "url": url, "model": mapped,
                "detail": str(e)[:300]}


# ---------------------------------------------------------------------------
# 密钥与配额管理
# ---------------------------------------------------------------------------

@router.get("/keys")
async def list_keys(_ops: dict = Depends(require_ops)):
    return {"success": True, "keys": key_svc.list_keys(), "models": ch_svc.all_models()}


@router.post("/keys")
async def create_key(body: KeyBody, ops: dict = Depends(require_ops)):
    try:
        info = key_svc.create_key(
            body.name, created_by=ops.get("username") or ops.get("id") or "",
            token_quota=body.token_quota, request_quota=body.request_quota,
            allowed_models=body.allowed_models, expires_at=body.expires_at)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 明文 key 仅此次返回
    return {"success": True, "key": info}


@router.put("/keys/{key_id}")
async def update_key(key_id: str, body: KeyPatch, _ops: dict = Depends(require_ops)):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    info = key_svc.update_key(key_id, patch)
    if not info:
        raise HTTPException(status_code=404, detail="密钥不存在")
    return {"success": True, "key": info}


@router.delete("/keys/{key_id}")
async def delete_key(key_id: str, _ops: dict = Depends(require_ops)):
    if not key_svc.delete_key(key_id):
        raise HTTPException(status_code=404, detail="密钥不存在")
    return {"success": True}


@router.post("/keys/{key_id}/reset")
async def reset_key(key_id: str, _ops: dict = Depends(require_ops)):
    info = key_svc.reset_usage(key_id)
    if not info:
        raise HTTPException(status_code=404, detail="密钥不存在")
    return {"success": True, "key": info}


# ---------------------------------------------------------------------------
# 请求日志与审计
# ---------------------------------------------------------------------------

@router.get("/logs")
async def query_logs(
    key_id: str = "", model: str = "", channel_id: str = "", status: str = "",
    risk_level: str = "", keyword: str = "", start: str = "", end: str = "",
    limit: int = 30, offset: int = 0, _ops: dict = Depends(require_ops),
):
    result = log_svc.query_logs(
        key_id=key_id, model=model, channel_id=channel_id, status=status,
        risk_level=risk_level, keyword=keyword, start=start, end=end,
        limit=limit, offset=offset)
    return {"success": True, **result}


@router.get("/logs/{log_id}")
async def get_log(log_id: str, _ops: dict = Depends(require_ops)):
    entry = log_svc.get_log(log_id)
    if not entry:
        raise HTTPException(status_code=404, detail="日志不存在")
    return {"success": True, "log": entry}


@router.delete("/logs")
async def purge_logs(_ops: dict = Depends(require_ops)):
    """按当前保留天数清理过期日志（手动触发）。"""
    days = int(settings_svc.get_settings().get("log_retention_days") or 30)
    n = log_svc.purge_old(days)
    return {"success": True, "deleted": n}


# ---------------------------------------------------------------------------
# 安全审计中心：规则 + 配置
# ---------------------------------------------------------------------------

@router.get("/rules")
async def list_rules(_ops: dict = Depends(require_ops)):
    """内置规则清单（含当前启用状态）+ 分类标签。"""
    settings = settings_svc.get_settings()
    disabled = set(settings.get("disabled_rules") or [])
    rules = security.list_builtin_rules()
    for r in rules:
        r["enabled"] = r["id"] not in disabled
        # 检测开关关闭时，该门控下的规则实际不生效
        gate = r.get("gate")
        r["effective"] = r["enabled"] and (not gate or bool(int(settings.get(gate, 1) or 0)))
    return {"success": True, "rules": rules,
            "category_labels": security.CATEGORY_LABELS,
            "audit_modes": list(security.AUDIT_MODES)}


@router.get("/settings")
async def get_settings(_ops: dict = Depends(require_ops)):
    return {"success": True, "settings": settings_svc.get_settings()}


@router.put("/settings")
async def save_settings(body: SettingsBody, _ops: dict = Depends(require_ops)):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    return {"success": True, "settings": settings_svc.save_settings(patch)}


@router.post("/scan-preview")
async def scan_preview(payload: dict, _ops: dict = Depends(require_ops)):
    """安全审计试跑：对给定文本/结构即时扫描，返回命中风险（不入库、不转发）。"""
    text = payload.get("text")
    settings = settings_svc.get_settings()
    if text is not None:
        result = security.scan_text(str(text), settings)
        return {"success": True, **result}
    obj = payload.get("body") or {}
    _, findings, risk_level, risk_score = security.scan_payload(obj, settings, do_mask=False)
    return {"success": True, "findings": findings,
            "risk_level": risk_level, "risk_score": risk_score}
